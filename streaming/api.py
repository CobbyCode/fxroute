# SPDX-License-Identifier: AGPL-3.0-only
"""Streaming provider HTTP API router: dispatcher, catalog, Qobuz auth, provider admin, device-name.

Extracted verbatim from main.py (Streaming-Dispatcher/Provider-Admin block L5275-6175).
Behavior, routes, status codes, headers and provider semantics are identical;
main.py keeps only the router registration via StreamingApiDeps.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, HTTPException, Request

import streaming
import streaming.activation as activation
from streaming.tidal import auth as tidal_auth
from streaming.tidal import playback as tidal_playback
from streaming.tidal.cache import library_cache as tidal_library_cache

# Helpers to honour test patches that target ``main.*`` (the historic
# location before the streaming extraction).  Handlers live in this module
# now, so ``mock.patch.object(main, "tidal_library_cache", store)`` would
# otherwise have no effect.  Resolve via ``sys.modules["main"]`` at call
# time when main is loaded.
def _resolve_tidal_cache():
    try:
        import sys

        main_mod = sys.modules.get("main")
        if main_mod is not None and hasattr(main_mod, "tidal_library_cache"):
            return getattr(main_mod, "tidal_library_cache")
    except Exception:
        pass
    return tidal_library_cache
import installer_contract as provider_contract
from http_errors import bad_request, internal_error
from audio import pw_link
from playback.transition import PlaybackTransitionFailure, TransitionRequest
from streaming.qobuz import connect_state
from streaming.spotify import connect_name as spotify_connect_name

logger = logging.getLogger(__name__)

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = BASE_DIR / "static"
PROVIDER_INSTALL_SCRIPT = BASE_DIR / "install.sh"
PROVIDER_UNINSTALL_SCRIPT = BASE_DIR / "uninstall.sh"
_PROVIDER_OP_TIMEOUT_SECONDS = 15 * 60
_PROVIDER_OP_OUTPUT_TAIL_CHARS = 4000
_PROVIDER_HELPER_MISSING_CONTRACT = (
    f"{provider_contract.DETAILS_KEY_HELPER_MISSING}="
    f"{provider_contract.DETAILS_VALUE_HELPER_MISSING}"
    f";{provider_contract.DETAILS_KEY_RERUN_INSTALL}="
    f"{provider_contract.DETAILS_VALUE_RERUN_INSTALL}"
)
_LOCAL_HOSTNAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_LOCAL_HOSTNAME_RESERVED = {"localhost"}
_SERVICE_RESTART_TERMINATE_GRACE_SECONDS = 3

_STREAMING_TRANSPORT_ACTIONS = {
    "play",
    "pause",
    "toggle",
    "next",
    "previous",
    "shuffle",
    "repeat",
}


@dataclass(frozen=True)
class StreamingApiDeps:
    """Application services injected from main.py (late-bound, no import of main)."""

    get_qobuz_ui_state: Callable[..., Awaitable[dict]]
    is_qobuz_playback_active: Callable[[dict], bool]
    qobuz_pause: Callable[[], Awaitable[dict]]
    broadcast_qobuz_state: Callable[..., Awaitable[dict]]
    qobuz_target_track_from_state: Callable[[dict], dict]
    qobuz_target_rate: Callable[[dict], int]
    qobuz_pin_unity: Callable[[], Awaitable[None]]
    qobuz_volume_action: Callable[[float], Awaitable[dict]]
    spotify_volume_action: Callable[[float], Awaitable[dict]]
    publish_committed_playback_owner: Callable[[str, Optional[str]], Awaitable[None]]
    transition_error_http: Callable[[Exception], HTTPException]
    request_origin_is_trusted: Callable[[Request], bool]
    coordinator_rate_change: Callable[[int], Any]
    run_coordinated_transition: Callable[[Any], Awaitable[Any]]
    get_current_playback_owner: Callable[[], Optional[str]]
    spotify_playerctl_watch: Any
    api_spotify_play: Callable[[], Awaitable[dict]]
    api_spotify_toggle: Callable[[], Awaitable[dict]]


@dataclass
class _StreamingApiRuntime:
    deps: Optional[StreamingApiDeps] = None


_runtime = _StreamingApiRuntime()


def configure_streaming_api(deps: StreamingApiDeps) -> None:
    """Bind the application services used by the route handlers."""
    _runtime.deps = deps


def register_streaming_routes(app, deps: StreamingApiDeps) -> None:
    """Register the streaming/provider-admin routes on the FastAPI application."""
    configure_streaming_api(deps)
    app.include_router(router)


def _deps() -> StreamingApiDeps:
    if _runtime.deps is None:
        raise RuntimeError("Streaming API runtime is not configured")
    return _runtime.deps


# ---------------------------------------------------------------------------
# Generic provider registry
# ---------------------------------------------------------------------------


@router.get("/api/streaming/providers")
async def api_streaming_providers():
    """List registered streaming providers with their capability surface.

    The generic foundation for a future streaming tab: the UI reads
    ``capabilities`` instead of branching on provider identity. Providers
    that are declared but not implemented report ``implemented=false`` and
    ``available=false`` and must never render as a usable service.
    """
    return {"providers": await streaming.describe_providers()}


@router.get("/api/streaming/providers/discovery")
async def api_streaming_provider_discovery():
    """Discover installed providers without runtime or remote probes."""
    return {"providers": await streaming.discover_providers()}


@router.get("/api/streaming/{provider_id}/status")
async def api_streaming_provider_status(provider_id: str):
    """Normalized provider/playback state for one registered provider."""
    provider = streaming.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"unknown streaming provider: {provider_id}")
    return await provider.status()


async def _qobuz_ui_start_action(action: str) -> dict:
    """Start Qobuz playback from the FXRoute UI through the source handoff.

    An action that brings Qobuz out of Paused/Stopped rides the same
    authoritative coordinator path as a Qobuz Connect claim: quiet the
    previous owner, establish rate/graph, start qbzd, commit
    ``playback_owner=qobuz`` and publish it on the playback broadcast.
    Toggling an already-playing Qobuz owner is transport-only, and replaying
    (``play``) an already-playing committed Qobuz owner is a no-op that never
    re-runs the handoff.  The 2s Qobuz Connect watcher stays responsible
    exclusively for external Connect claims; a UI start never waits for it.
    """
    deps = _deps()
    qobuz_state = await deps.get_qobuz_ui_state()
    if action == "toggle" and deps.is_qobuz_playback_active(qobuz_state):
        data = await deps.qobuz_pause()
        return await deps.broadcast_qobuz_state(data)
    if (
        action == "play"
        and deps.get_current_playback_owner() == "qobuz"
        and deps.is_qobuz_playback_active(qobuz_state)
    ):
        # Qobuz is already the committed, playing owner: a repeated play would
        # re-handoff a live renderer and unnecessarily perturb the stream.
        return qobuz_state
    track = deps.qobuz_target_track_from_state(qobuz_state)
    target_rate = deps.qobuz_target_rate(qobuz_state)
    qobuz_rate_change = await asyncio.to_thread(deps.coordinator_rate_change, target_rate)
    request = TransitionRequest(
        operation="qobuz-play" if action == "play" else "qobuz-toggle",
        source="qobuz",
        target_rate=target_rate,
        target_url=str(track.get("id") or ""),
        target_track=track,
        should_play=True,
        rate_change=qobuz_rate_change,
        reload_source=True,
        detail=f"api-streaming-qobuz-{action}",
    )
    try:
        result = await deps.run_coordinated_transition(request)
    except ValueError as exc:
        raise bad_request(exc) from exc
    except PlaybackTransitionFailure as exc:
        raise deps.transition_error_http(exc) from exc
    if not getattr(result, "committed", False):
        return await deps.broadcast_qobuz_state()
    await deps.publish_committed_playback_owner("qobuz", getattr(result, "transition_id", None))
    connect_state.set_device_active(True)
    await deps.qobuz_pin_unity()
    return await deps.broadcast_qobuz_state()


@router.post("/api/streaming/{provider_id}/{action}")
async def api_streaming_provider_action(provider_id: str, action: str, request: Request):
    """Generic provider transport action, dispatched by capability.

    This is the provider-level transport contract (no FXRoute source
    transition). The existing ``/api/spotify/*`` endpoints keep their source
    handoff semantics for Spotify. Spotify and Qobuz start actions
    (``play``/``toggle`` out of Paused/Stopped) are routed through the
    authoritative source handoff instead of the raw provider transport;
    all other actions stay transport-only on the provider.
    """
    deps = _deps()
    provider = streaming.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"unknown streaming provider: {provider_id}")
    if provider_id == "spotify" and action == "play":
        return await deps.api_spotify_play()
    if provider_id == "spotify" and action == "toggle":
        return await deps.api_spotify_toggle()
    if provider_id == "qobuz" and action in ("play", "toggle"):
        return await _qobuz_ui_start_action(action)
    if action == "seek":
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return await provider.seek(float(body.get("position", 0)))
        except streaming.ProviderNotImplemented as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
    if action == "volume":
        try:
            body = await request.json()
        except Exception:
            body = {}
        if provider_id == "qobuz":
            # Qobuz volume is the canonical FXRoute master (qbzd gain stays
            # pinned at 100%).
            return await deps.qobuz_volume_action(float(body.get("volume", 100)))
        if provider_id == "spotify":
            # spotifyd runs with volume_controller=none: its Connect volume is
            # a reported value only, so the slider drives the FXRoute master.
            return await deps.spotify_volume_action(float(body.get("volume", 100)))
        try:
            return await provider.set_volume(float(body.get("volume", 100)))
        except streaming.ProviderNotImplemented as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
    if action not in _STREAMING_TRANSPORT_ACTIONS:
        raise HTTPException(status_code=404, detail=f"unknown streaming action: {action}")
    method = getattr(provider, action)
    try:
        return await method()
    except streaming.ProviderNotImplemented as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# TIDAL (native provider: auth + catalog; playback rides /api/play source=tidal)
# ---------------------------------------------------------------------------


def _streaming_provider(provider_id: str):
    # Honour ``mock.patch.object(main, "_streaming_provider", ...)`` used by
    # ``scripts/test_tidal_library_cache.py``.  The extraction moved the
    # real provider lookup here; tests still patch ``main``.
    try:
        import sys
        import unittest.mock as _mock

        main_mod = sys.modules.get("main")
        if main_mod is not None:
            cand = getattr(main_mod, "_streaming_provider", None)
            if cand is not None and isinstance(cand, _mock.Mock):
                return cand(provider_id)
    except Exception:
        pass
    provider = streaming.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"unknown streaming provider: {provider_id}")
    return provider


def _tidal_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, tidal_auth.TidalAuthError):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, tidal_playback.TidalStreamError):
        status = {
            tidal_playback.KIND_AUTH: 401,
            tidal_playback.KIND_RIGHTS: 403,
            tidal_playback.KIND_UNAVAILABLE: 404,
            tidal_playback.KIND_NETWORK: 502,
            tidal_playback.KIND_UNSUPPORTED: 501,
        }.get(exc.kind, 500)
        return HTTPException(status_code=status, detail=str(exc))
    return internal_error("Streaming provider request failed", exc)


def _provider_catalog_method(provider_id: str, name: str):
    provider = _streaming_provider(provider_id)
    fn = getattr(provider, name, None)
    if not callable(fn):
        raise HTTPException(status_code=501, detail=f"provider {provider_id} does not implement {name}")
    return fn


@router.get("/api/streaming/{provider_id}/search")
async def api_streaming_provider_search(provider_id: str, q: str = "", types: str | None = None, limit: int = 25):
    fn = _provider_catalog_method(provider_id, "search")
    type_list = [t.strip() for t in (types or "").split(",") if t.strip()]
    try:
        return await fn(q, type_list, limit)
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.get("/api/streaming/{provider_id}/favorites")
async def api_streaming_provider_favorites(
    provider_id: str, limit: int = 50, type: str = "tracks"
):
    """Favorites for one catalog category (tracks/albums/artists)."""
    category = (type or "tracks").strip().lower()
    method_name = {
        "tracks": "favorites",
        "albums": "favorites_albums",
        "artists": "favorites_artists",
    }.get(category)
    if method_name is None:
        raise HTTPException(status_code=400, detail=f"unsupported favorites category: {type}")
    fn = _provider_catalog_method(provider_id, method_name)
    try:
        return await fn(limit)
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.get("/api/streaming/{provider_id}/playlists")
async def api_streaming_provider_playlists(provider_id: str):
    fn = _provider_catalog_method(provider_id, "playlists")
    try:
        return await fn()
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.get("/api/streaming/{provider_id}/playlists/{playlist_id}/tracks")
async def api_streaming_provider_playlist_tracks(provider_id: str, playlist_id: str):
    fn = _provider_catalog_method(provider_id, "playlist_tracks")
    try:
        return await fn(playlist_id)
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.get("/api/streaming/{provider_id}/playlists/{playlist_id}")
async def api_streaming_provider_playlist(provider_id: str, playlist_id: str):
    """Playlist detail: description, distinct artists and single-artist enrichment."""
    fn = _provider_catalog_method(provider_id, "get_playlist")
    try:
        return await fn(playlist_id)
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.get("/api/streaming/{provider_id}/albums/{album_id}/tracks")
async def api_streaming_provider_album_tracks(provider_id: str, album_id: str):
    fn = _provider_catalog_method(provider_id, "get_album_tracks")
    try:
        return await fn(album_id)
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.get("/api/streaming/{provider_id}/albums/{album_id}")
async def api_streaming_provider_album(provider_id: str, album_id: str):
    fn = _provider_catalog_method(provider_id, "get_album")
    try:
        return await fn(album_id)
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.get("/api/streaming/{provider_id}/artists/{artist_id}")
async def api_streaming_provider_artist(provider_id: str, artist_id: str):
    """Artist detail with albums and top tracks."""
    fn = _provider_catalog_method(provider_id, "get_artist")
    try:
        return await fn(artist_id)
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.get("/api/streaming/{provider_id}/favorites/ids")
async def api_streaming_provider_favorite_ids(provider_id: str):
    """Authoritative favorited track/album ids for the heart state."""
    fn = _provider_catalog_method(provider_id, "favorite_state")
    try:
        return await fn()
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.get("/api/streaming/tidal/library/snapshot")
async def api_tidal_library_snapshot(user: str = ""):
    """Last successfully fetched TIDAL library/browse state (cache-only).

    Reads the per-account SQLite cache without any TIDAL network traffic, so
    the UI can render the last-known albums/tracks/artists/playlists/favorite
    ids immediately and refresh in the background afterwards.  A ``user`` id
    that has no cache yet yields an empty payload (``ids`` empty, all lists
    empty), never an error — the UI treats that as \"nothing cached yet\".
    """
    user_id = (user or "").strip()
    empty = {
        "user_id": user_id,
        "fetched_at": None,
        "ids": {},
        "tracks": [],
        "albums": [],
        "artists": [],
        "playlists": [],
    }
    if not user_id:
        return empty
    cache = _resolve_tidal_cache()
    snapshot = cache.snapshot(user_id)
    return snapshot if snapshot is not None else empty


@router.post("/api/streaming/{provider_id}/tracks/{track_id}/favorite")
async def api_streaming_provider_track_favorite(provider_id: str, track_id: str, request: Request):
    fn = _provider_catalog_method(provider_id, "set_track_favorite")
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        return await fn(track_id, bool(body.get("favorite", False)))
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.post("/api/streaming/{provider_id}/albums/{album_id}/favorite")
async def api_streaming_provider_album_favorite(provider_id: str, album_id: str, request: Request):
    fn = _provider_catalog_method(provider_id, "set_album_favorite")
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        return await fn(album_id, bool(body.get("favorite", False)))
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.post("/api/streaming/{provider_id}/artists/{artist_id}/favorite")
async def api_streaming_provider_artist_favorite(provider_id: str, artist_id: str, request: Request):
    fn = _provider_catalog_method(provider_id, "set_artist_favorite")
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        return await fn(artist_id, bool(body.get("favorite", False)))
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.post("/api/streaming/{provider_id}/playlists/{playlist_id}/favorite")
async def api_streaming_provider_playlist_favorite(provider_id: str, playlist_id: str, request: Request):
    fn = _provider_catalog_method(provider_id, "set_playlist_favorite")
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        return await fn(playlist_id, bool(body.get("favorite", False)))
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.post("/api/streaming/{provider_id}/playlists/create")
async def api_streaming_provider_create_playlist(provider_id: str, request: Request):
    """Create a TIDAL playlist, optionally seeded with the given tracks."""
    fn = _provider_catalog_method(provider_id, "create_playlist")
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="playlist name is required")
    track_ids = [str(i) for i in (body.get("track_ids") or []) if str(i).strip()]
    try:
        return await fn(name, str(body.get("description", "")), track_ids)
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.post("/api/streaming/{provider_id}/playlists/{playlist_id}/tracks")
async def api_streaming_provider_add_playlist_tracks(provider_id: str, playlist_id: str, request: Request):
    """Add tracks to an existing TIDAL playlist."""
    fn = _provider_catalog_method(provider_id, "add_playlist_tracks")
    try:
        body = await request.json()
    except Exception:
        body = {}
    track_ids = [str(i) for i in (body.get("track_ids") or []) if str(i).strip()]
    if not track_ids:
        raise HTTPException(status_code=400, detail="track_ids is required")
    try:
        return await fn(playlist_id, track_ids)
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.post("/api/streaming/tidal/auth/device")
async def api_tidal_start_device_login():
    provider = _streaming_provider("tidal")
    try:
        return await provider.start_device_login()
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.post("/api/streaming/tidal/auth/device/finish")
async def api_tidal_finish_device_login():
    provider = _streaming_provider("tidal")
    try:
        return await provider.finish_device_login()
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.post("/api/streaming/tidal/auth/pkce")
async def api_tidal_pkce_login_url():
    provider = _streaming_provider("tidal")
    try:
        return {"url": await provider.pkce_login_url()}
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.post("/api/streaming/tidal/auth/pkce/finish")
async def api_tidal_finish_pkce_login(request: Request):
    provider = _streaming_provider("tidal")
    try:
        body = await request.json()
    except Exception:
        body = {}
    redirect_url = str(body.get("redirect_url") or body.get("url") or "")
    if not redirect_url:
        raise HTTPException(status_code=400, detail="redirect_url is required")
    try:
        return await provider.finish_pkce_login(redirect_url)
    except Exception as exc:
        raise _tidal_http_error(exc) from exc


@router.post("/api/streaming/tidal/auth/logout")
async def api_tidal_logout():
    provider = _streaming_provider("tidal")
    await provider.logout()
    return {"authenticated": False}


# ---------------------------------------------------------------------------
# Qobuz/qbzd account login (browser OAuth handoff owned by qbzd itself)
# ---------------------------------------------------------------------------


def _qobuz_provider_or_404():
    provider = streaming.get_provider("qobuz")
    if provider is None:
        raise HTTPException(status_code=404, detail="qobuz provider is not registered")
    return provider


@router.get("/api/streaming/qobuz/auth/state")
async def api_qobuz_auth_state():
    """Qobuz account state: qbzd auth plus any in-flight browser login."""
    provider = _qobuz_provider_or_404()
    return {
        "installed": provider.is_installed(),
        "authenticated": await provider.is_authenticated(),
        "login": await provider.login_state(),
    }


@router.post("/api/streaming/qobuz/auth/login")
async def api_qobuz_auth_login(request: Request):
    """Start (or re-enter) the qbzd browser OAuth flow.

    Works for both a first login and an account switch: qbzd's login replaces
    the stored credential on completion.
    """
    if not _deps().request_origin_is_trusted(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    provider = _qobuz_provider_or_404()
    if not provider.is_installed():
        raise HTTPException(status_code=409, detail="qbzd is not installed; install the Qobuz provider first")
    try:
        return await provider.begin_login()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/api/streaming/qobuz/auth/login/finish")
async def api_qobuz_auth_login_finish(request: Request):
    """Complete the qbzd browser OAuth flow with the pasted redirect URL."""
    if not _deps().request_origin_is_trusted(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    provider = _qobuz_provider_or_404()
    try:
        body = await request.json()
    except Exception:
        body = {}
    pasted = str(body.get("redirect_url") or body.get("url") or body.get("code") or "")
    try:
        return await provider.finish_login(pasted)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/streaming/qobuz/auth/login/cancel")
async def api_qobuz_auth_login_cancel(request: Request):
    """Abort an in-flight qbzd browser login (terminates the CLI listener)."""
    if not _deps().request_origin_is_trusted(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    provider = _qobuz_provider_or_404()
    try:
        return await provider.cancel_login()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/api/streaming/qobuz/auth/logout")
async def api_qobuz_auth_logout(request: Request):
    """Clear the qbzd credential (account disconnect / reset)."""
    if not _deps().request_origin_is_trusted(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    provider = _qobuz_provider_or_404()
    try:
        return await provider.logout()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Provider administration (Settings -> Providers)
# ---------------------------------------------------------------------------


def _provider_op_log_tail(result: dict) -> str:
    """Return the combined operator-facing output tail of an installer run."""
    combined = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}".strip()
    if len(combined) > _PROVIDER_OP_OUTPUT_TAIL_CHARS:
        combined = combined[-_PROVIDER_OP_OUTPUT_TAIL_CHARS:]
    return combined


async def _run_provider_installer_op(script: Path, label: str, *args: str) -> dict:
    """Run the existing installer/uninstaller for one provider operation.

    The subprocess lives in its own session so a timeout/cancel can signal the
    whole child tree, mirroring the bounded update-script execution path.
    Provider installs run fully unprivileged (no sudo here): the only root
    steps are the fixed actions of the root-owned helper
    ``/usr/local/sbin/fxroute-provider-privileged``, which the full
    installer sets up. A missing helper therefore means \"rerun the full
    install.sh once\", reported as 503 instead of hanging on a TTY prompt.
    """
    if not script.exists():
        raise HTTPException(status_code=500, detail=f"Installer script missing: {script}")
    try:
        proc = await asyncio.create_subprocess_exec(
            str(script),
            *args,
            cwd=str(BASE_DIR),
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"Installer script missing: {script}") from None
    communicate_task = asyncio.create_task(proc.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.shield(communicate_task), timeout=_PROVIDER_OP_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        if await pw_link.stop_process_group_cancellation_safe(proc, communicate_task, grace_seconds=5):
            raise asyncio.CancelledError
        try:
            stdout, stderr = communicate_task.result()
        except Exception:
            stdout, stderr = b"", b""
        raise HTTPException(status_code=504, detail=f"{label} timed out") from None
    except asyncio.CancelledError:
        await pw_link.stop_process_group_cancellation_safe(proc, communicate_task, grace_seconds=5)
        raise
    result = {
        "returncode": proc.returncode,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    }
    # Machine-readable contract: install.sh prints a PROVIDER_CONTRACT=
    # marker on stdout when the helper is missing; prose stays for the
    # operator only and is never parsed. The machine pair travels in the
    # X-FXRoute-Provider-Contract header; the visible detail is unchanged.
    log_tail = _provider_op_log_tail(result)
    if provider_contract.MARKER_HELPER_MISSING in log_tail:
        raise HTTPException(
            status_code=503,
            headers={"X-FXRoute-Provider-Contract": _PROVIDER_HELPER_MISSING_CONTRACT},
            detail=(
                f"{label} needs the provider privilege helper: rerun the full "
                "install.sh once, then retry the install from Settings"
            ),
        ) from None
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"{label} failed: {_provider_op_log_tail(result) or f'exit code {proc.returncode}'}",
        )
    return result


@router.get("/api/streaming/providers/admin")
async def api_streaming_providers_admin():
    """Operator-facing provider administration state for Settings -> Providers."""
    described = await streaming.describe_providers()
    providers = []
    for entry in described:
        provider_id = str(entry.get("id") or "")
        providers.append(
            {
                "id": provider_id,
                "name": entry.get("name") or provider_id,
                "installed": bool(entry.get("installed")),
                "available": bool(entry.get("available")),
                "authenticated": entry.get("authenticated"),
                "enabled": bool(entry.get("enabled", True)),
                "implemented": bool(entry.get("implemented", True)),
            }
        )
    return {
        "providers": providers,
        "device_name": _mdns_device_name(),
        "device_name_can_change": shutil.which("hostnamectl") is not None,
    }


@router.post("/api/streaming/providers/{provider_id}/enabled")
async def api_streaming_provider_set_enabled(provider_id: str, request: Request):
    """Enable or disable a provider (visibility only; never installs/uninstalls)."""
    if not _deps().request_origin_is_trusted(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    if streaming.get_provider(provider_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown streaming provider: {provider_id}")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body.get("enabled"), bool):
        raise HTTPException(status_code=400, detail="enabled (boolean) is required")
    streaming.activation.set_enabled(provider_id, body["enabled"])
    return {"id": provider_id, "enabled": body["enabled"]}


def _refresh_tidalapi_import_verdict() -> None:
    """Re-evaluate streaming.tidal.auth's cached tidalapi import verdict.

    tidalapi is imported once at module load; pip installing/removing it in
    the same venv is invisible to the running process until the module is
    reloaded. A bare importlib.reload is not enough after an uninstall: the
    stale module still sits in sys.modules, so ``import tidalapi`` inside
    the reload would succeed spuriously. Purge the cached modules first.
    """

    import streaming.tidal.auth as _tidal_auth

    for name in list(sys.modules):
        if name == "tidalapi" or name.startswith("tidalapi."):
            del sys.modules[name]
    importlib.reload(_tidal_auth)


@router.post("/api/streaming/providers/{provider_id}/install")
async def api_streaming_provider_install(provider_id: str, request: Request):
    """Install a provider's backend via the existing installer path."""
    deps = _deps()
    if not deps.request_origin_is_trusted(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    if streaming.get_provider(provider_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown streaming provider: {provider_id}")
    flag = {
        "spotify": "--spotifyd",
        "qobuz": "--qobuz",
        "tidal": "--tidal",
    }.get(provider_id)
    if flag is None:
        raise HTTPException(status_code=400, detail=f"provider {provider_id} cannot be installed from the UI")
    result = await _run_provider_installer_op(
        PROVIDER_INSTALL_SCRIPT, f"{provider_id} install", "--providers-only", flag, "--yes"
    )
    if provider_id == "spotify" and streaming.get_provider(provider_id).is_installed():
        # A late Settings install must enable the MPRIS claim watch without
        # an FXRoute restart: image installs start FXRoute before providers,
        # so the watch may still be in its availability wait (or a previous
        # run may have ended). Rearm it here; the waiting poll is the fallback.
        try:
            deps.spotify_playerctl_watch.notify_provider_installed()
            if deps.spotify_playerctl_watch.watch_task is None or deps.spotify_playerctl_watch.watch_task.done():
                deps.spotify_playerctl_watch.last_trigger_at = 0.0
                deps.spotify_playerctl_watch.watch_task = asyncio.create_task(
                    deps.spotify_playerctl_watch.run_watch_loop(),
                    name="spotify-playerctl-watch",
                )
                logger.info("Spotify playerctl watch (re)started after provider install")
        except Exception as exc:
            logger.warning("Could not rearm Spotify watch after install: %s", exc)
    if provider_id == "tidal":
        # tidalapi is imported once at module load (streaming.tidal.auth); a
        # fresh pip install in the same venv is invisible to the running
        # process until the module re-imports it. Refresh so the response
        # (and the admin/status endpoints served by this process) report the
        # new state honestly instead of a false \"installed\": false until the
        # next FXRoute restart.
        try:
            _refresh_tidalapi_import_verdict()
            if not streaming.get_provider("tidal").is_installed():
                logger.warning(
                    "TIDAL install finished but tidalapi is still not importable "
                    "in the running process; state may read as not installed "
                    "until the next FXRoute restart"
                )
        except Exception:
            logger.warning("TIDAL provider module refresh failed", exc_info=True)
    return {
        "ok": True,
        "provider_id": provider_id,
        "log": _provider_op_log_tail(result),
        "installed": streaming.get_provider(provider_id).is_installed(),
    }


@router.post("/api/streaming/providers/{provider_id}/uninstall")
async def api_streaming_provider_uninstall(provider_id: str, request: Request):
    """Uninstall a provider's backend via the existing uninstaller (explicit action)."""
    if not _deps().request_origin_is_trusted(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    if streaming.get_provider(provider_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown streaming provider: {provider_id}")
    if provider_id not in {"spotify", "qobuz", "tidal"}:
        raise HTTPException(status_code=400, detail=f"provider {provider_id} cannot be uninstalled from the UI")
    result = await _run_provider_installer_op(
        PROVIDER_UNINSTALL_SCRIPT, f"{provider_id} uninstall", "--provider", provider_id, "--yes"
    )
    if provider_id == "tidal":
        # Mirror of the install path: pip-uninstalling tidalapi behind the
        # running process leaves the module's import verdict stale; refresh
        # so uninstall reports honestly instead of a false \"installed\": true.
        try:
            _refresh_tidalapi_import_verdict()
        except Exception:
            logger.warning("TIDAL provider module refresh failed after uninstall", exc_info=True)
    return {
        "ok": True,
        "provider_id": provider_id,
        "log": _provider_op_log_tail(result),
        "installed": streaming.get_provider(provider_id).is_installed(),
    }


@router.post("/api/streaming/providers/{provider_id}/service/{action}")
async def api_streaming_provider_service_action(provider_id: str, action: str, request: Request):
    """Start/stop/restart a provider's user service (Connect readiness)."""
    if not _deps().request_origin_is_trusted(request):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    unit = {"spotify": "spotifyd.service", "qobuz": "qbzd.service"}.get(provider_id)
    if unit is None:
        raise HTTPException(status_code=400, detail=f"provider {provider_id} has no service to control")
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(status_code=404, detail=f"unknown service action: {action}")
    proc = await asyncio.create_subprocess_exec(
        "systemctl",
        "--user",
        action,
        unit,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    communicate_task = asyncio.create_task(proc.communicate())
    try:
        _stdout, stderr = await asyncio.wait_for(asyncio.shield(communicate_task), timeout=15)
    except asyncio.TimeoutError:
        if await pw_link.stop_command_child_cancellation_safe(
            proc, _SERVICE_RESTART_TERMINATE_GRACE_SECONDS
        ):
            raise asyncio.CancelledError
        try:
            _stdout, stderr = communicate_task.result()
        except Exception:
            _stdout, stderr = b"", b""
        raise HTTPException(status_code=504, detail="systemctl timed out") from None
    except asyncio.CancelledError:
        await pw_link.stop_command_child_cancellation_safe(proc, _SERVICE_RESTART_TERMINATE_GRACE_SECONDS)
        raise
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or f"systemctl {action} {unit} failed"
        raise HTTPException(status_code=500, detail=detail)
    provider = streaming.get_provider(provider_id)
    return {
        "ok": True,
        "provider_id": provider_id,
        "unit": unit,
        "action": action,
        "installed": bool(provider.is_installed()) if provider else False,
        "available": bool(await provider.is_available()) if provider else False,
    }


def _mdns_device_name() -> str:
    """Return the current .local device name (system hostname)."""
    return socket.gethostname().strip().strip(".").lower()


async def _restart_spotifyd_best_effort() -> bool:
    """Restart the spotifyd user service; never raises, returns success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "restart",
            "spotifyd.service",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=15)
        return proc.returncode == 0
    except (OSError, asyncio.TimeoutError):
        return False


async def _sync_spotify_connect_name_best_effort() -> dict:
    """Align the managed spotifyd name; restarts spotifyd only on change."""
    try:
        result = await asyncio.to_thread(spotify_connect_name.sync_spotifyd_device_name)
    except Exception as exc:
        logger.warning("Spotify Connect name sync skipped: %s", exc)
        return {"changed": False}
    if result.get("changed"):
        logger.info("Spotify Connect name set to %s", result.get("desired"))
        result["restarted"] = await _restart_spotifyd_best_effort()
    return result

