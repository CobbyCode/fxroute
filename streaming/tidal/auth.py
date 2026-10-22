# SPDX-License-Identifier: AGPL-3.0-only

"""TIDAL OAuth session management.

Owns the ``tidalapi`` session lifecycle: creating a session, running the
browser PKCE login, persisting the OAuth session to the FXRoute config
directory and reloading/refreshing it.

FXRoute never sees the TIDAL password: the login hands the user a URL and only
the resulting OAuth tokens are stored. A session file written by the removed
device login still loads and refreshes like any other.
The session file lives in ``~/.config/fxroute/tidal-session.json`` (the same
config dir used by stations.json / playlists.json), never in ``.env``.

``tidalapi`` is imported lazily so the provider package stays importable on
hosts without it (the provider then reports ``available=false``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    import tidalapi
except ImportError:  # pragma: no cover - exercised only where tidalapi is absent
    tidalapi = None  # type: ignore[assignment]


class TidalAuthError(RuntimeError):
    """Raised when TIDAL authentication cannot proceed or fails."""


def tidalapi_available() -> bool:
    """Return whether the ``tidalapi`` package is importable."""
    return tidalapi is not None


def _is_auth_rejection(exc: BaseException) -> bool:
    """Return whether ``exc`` is tidalapi's rejected-refresh-token error.

    ``tidalapi`` refreshes an expired access token inside the request layer and
    raises ``AuthenticationError`` when TIDAL rejects the refresh token. That is
    a lost login, not a transient outage.
    """
    error_type = getattr(getattr(tidalapi, "exceptions", None), "AuthenticationError", None)
    return isinstance(error_type, type) and isinstance(exc, error_type)


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "fxroute"


SESSION_FILE = _config_dir() / "tidal-session.json"

# Default requested stream quality. TIDAL downgrades gracefully: HI_RES_LOSSLESS
# yields 24-bit FLAC where available and 16-bit FLAC otherwise (both require the
# PKCE login).
DEFAULT_QUALITY = "HI_RES_LOSSLESS"


def _new_session(quality: str | None = None) -> "tidalapi.Session":
    if tidalapi is None:
        raise TidalAuthError("tidalapi is not installed")
    session = tidalapi.Session()
    session.config.quality = quality or DEFAULT_QUALITY
    # tidalapi labels every non-200 token refresh as AuthenticationError,
    # including server outages. Preserve those HTTP failures before it parses
    # the response, without changing its invalid_grant / rejected-token path.
    def preserve_refresh_transport_error(response, *args, **kwargs):
        is_refresh_endpoint = response.url == session.config.api_oauth2_token
        is_transient_failure = response.status_code == 429 or 500 <= response.status_code < 600
        if is_refresh_endpoint and is_transient_failure:
            response.raise_for_status()

    request_session = getattr(session, "request_session", None)
    if request_session is not None:
        request_session.hooks["response"].append(preserve_refresh_transport_error)
    return session


class TidalSession:
    """Holds the active tidalapi session and the pending login state.

    A single instance is shared by the provider; the application is
    single-user, so one session (and one pending login at a time) is
    enough. Auth mutations run under an asyncio lock so a login start and
    finish never race. The pending login is guarded by an auth generation:
    starting a new login or logging out invalidates it, and a superseded
    attempt never commits.
    """

    def __init__(self) -> None:
        self._session: Any | None = None
        self._pending_session: Any | None = None  # session of the PKCE login in progress
        self._auth_generation = 0
        # Last successfully verified user payload; kept across transient TIDAL
        # outages so the UI never flips to login just because a check failed.
        self._last_user: dict | None = None
        self._lock = asyncio.Lock()

    # -- session access -----------------------------------------------------

    def session(self, *, require_login: bool = False) -> Any | None:
        """Return the active session, or None when not authenticated."""
        if self._session is None:
            self._session = self._load_session()
        if require_login and self._session is not None and not self._session.check_login():
            self._session = None
        return self._session

    async def get_session(self) -> Any:
        """Return the active session, offloading restoration to a thread."""
        return await asyncio.to_thread(self.session)

    def authenticated(self) -> bool:
        """Return whether the session is authenticated, resilient to outages.

        A verified login check refreshes the last-known user payload; a
        transient transport failure (TIDAL unreachable) keeps the last known
        good state instead of logging the user out, while an explicit auth
        rejection (revoked/expired token) clears it.  Server-side failures
        (5xx) are treated as transient too.
        """
        s = self.session()
        if s is None or getattr(s, "access_token", None) is None:
            return False
        user = getattr(s, "user", None)
        user_id = getattr(user, "id", None) if user is not None else None
        basic = getattr(getattr(s, "request", None), "basic_request", None)
        if not callable(basic) or not user_id:
            return self._authenticated_fallback(s)
        try:
            resp = basic("GET", "users/%s/subscription" % user_id)
        except Exception as exc:  # noqa: BLE001 - network/transport failures
            if _is_auth_rejection(exc):
                logger.info("TIDAL rejected the refresh token; a new login is required")
                self._last_user = None
                return False
            logger.debug("TIDAL login check failed transiently: %s", exc)
            return bool(self._last_user)
        status = int(getattr(resp, "status_code", 0) or 0)
        if resp is not None and getattr(resp, "ok", False):
            self._last_user = self._read_user_payload(s, user)
            return True
        if 500 <= status < 600:
            # TIDAL server trouble is not an auth failure.
            return bool(self._last_user)
        self._last_user = None
        return False

    def _authenticated_fallback(self, s: Any) -> bool:
        """Login check via ``check_login`` for older tidalapi request layers."""
        try:
            ok = bool(s.check_login())
        except Exception as exc:  # noqa: BLE001
            if _is_auth_rejection(exc):
                logger.info("TIDAL rejected the refresh token; a new login is required")
                self._last_user = None
                return False
            logger.debug("TIDAL login check failed transiently: %s", exc)
            return bool(self._last_user)
        if ok:
            user = getattr(s, "user", None)
            self._last_user = self._read_user_payload(s, user)
            return True
        self._last_user = None
        return False

    @staticmethod
    def _read_user_payload(s: Any, user: Any) -> dict:
        """Normalized last-known user info from the session/user objects."""
        user_id = getattr(user, "id", None) if user is not None else None
        return {
            "id": str(user_id) if user_id is not None else None,
            "email": str(getattr(user, "email", "") or "") if user is not None else "",
            "country_code": str(getattr(s, "country_code", "") or "") or None,
        }

    def last_user_id(self) -> str:
        """Last verified TIDAL user id ('' when unknown); never touches the network."""
        if self._last_user:
            return str(self._last_user.get("id") or "")
        return ""

    def user_payload(self) -> dict:
        """Last-known user payload without network access.

        The verified payload from :meth:`authenticated` is authoritative;
        before the first check the session object's user is used (only when
        already materialized, never triggering a fetch).
        """
        if self._last_user:
            return dict(self._last_user)
        s = self.session()
        if s is None:
            return {"id": None, "email": "", "country_code": None}
        try:
            user = getattr(s, "user", None)
        except Exception:  # noqa: BLE001 - lazy user fetch can hit the network
            user = None
        if user is not None:
            self._last_user = self._read_user_payload(s, user)
            return dict(self._last_user)
        return {"id": None, "email": "", "country_code": None}

    async def user_payload_async(self) -> dict:
        return await asyncio.to_thread(self.user_payload)

    async def is_authenticated(self) -> bool:
        """Offload the (network-backed) login check off the event loop."""
        return await asyncio.to_thread(self.authenticated)

    async def clear(self) -> None:
        """Forget the in-memory session and remove the persisted token file."""
        user_id = ""
        try:
            if self._last_user:
                user_id = str(self._last_user.get("id") or "")
        except Exception:  # noqa: BLE001 - defensive; logout must not fail
            user_id = ""
        if not user_id:
            try:
                if SESSION_FILE.exists():
                    raw = json.loads(SESSION_FILE.read_text())
                    user = raw.get("user") if isinstance(raw, dict) else None
                    if isinstance(user, dict):
                        user_id = str(user.get("id") or "")
            except Exception:  # noqa: BLE001 - corrupted file still logs out
                user_id = ""
        if not user_id and self._session is not None:
            try:
                user = getattr(self._session, "user", None)
                uid = getattr(user, "id", None)
                if uid is not None:
                    user_id = str(uid)
            except Exception:  # noqa: BLE001 - lazy user may hit network
                user_id = ""
        async with self._lock:
            self._auth_generation += 1
            self._session = None
            self._pending_session = None
            self._last_user = None
            try:
                if SESSION_FILE.exists():
                    SESSION_FILE.unlink()
            except OSError as exc:
                logger.warning("Failed to remove TIDAL session file: %s", exc)
        if user_id:
            try:
                from streaming.tidal.cache import library_cache

                library_cache.clear_user(user_id)
            except Exception as exc:  # noqa: BLE001 - cache must not break logout
                logger.warning("Failed to clear TIDAL library cache for %s: %s", user_id, exc)

    # -- PKCE login (browser; required for Hi-Res 24-bit FLAC) --------------

    @staticmethod
    def _create_pkce_login() -> tuple[Any, str]:
        if tidalapi is None:
            raise TidalAuthError("tidalapi is not installed")
        session = _new_session(DEFAULT_QUALITY)
        return session, session.pkce_login_url()

    def pkce_login_url(self) -> str:
        """Return the browser login URL for the PKCE flow.

        The user opens it, signs in, and is redirected to an "Oops" page whose
        full URL must be pasted back via :meth:`finish_pkce_login`. Starting a
        new login invalidates the pending one.
        """
        if tidalapi is None:
            raise TidalAuthError("tidalapi is not installed")
        session = _new_session(DEFAULT_QUALITY)
        self._auth_generation += 1
        self._pending_session = session
        return session.pkce_login_url()

    @staticmethod
    def _exchange_pkce_login(session: Any, redirect_url: str) -> dict:
        try:
            token = session.pkce_get_auth_token(redirect_url)
            session.process_auth_token(token, is_pkce_token=True)
        except Exception as exc:  # noqa: BLE001
            raise TidalAuthError(f"PKCE login failed: {exc}") from exc
        if not session.check_login():
            raise TidalAuthError("PKCE login did not produce a valid session")
        return _session_payload(session)

    def finish_pkce_login(self, redirect_url: str) -> dict:
        """Exchange the pasted PKCE redirect URL for an authenticated session.

        The pasted URL is the TIDAL "Oops" redirect page the browser lands on;
        ``pkce_get_auth_token`` extracts its ``code`` and exchanges it for
        tokens, which ``process_auth_token`` then applies to the session.
        A superseded attempt (newer login or logout started meanwhile) never
        commits.
        """
        if self._pending_session is None:
            raise TidalAuthError("no PKCE login is in progress")
        session = self._pending_session
        generation = self._auth_generation
        self._pending_session = None
        payload = self._exchange_pkce_login(session, redirect_url)
        if generation != self._auth_generation:
            raise TidalAuthError("PKCE login was superseded")
        self._session = session
        self._last_user = payload
        self._save_session(session)
        return self._last_user

    async def pkce_login_url_async(self) -> str:
        async with self._lock:
            session, url = await asyncio.to_thread(self._create_pkce_login)
            self._auth_generation += 1
            self._pending_session = session
            return url

    async def finish_pkce_login_async(self, redirect_url: str) -> dict:
        async with self._lock:
            if self._pending_session is None:
                raise TidalAuthError("no PKCE login is in progress")
            session = self._pending_session
            generation = self._auth_generation
            self._pending_session = None
        payload = await asyncio.to_thread(self._exchange_pkce_login, session, redirect_url)
        async with self._lock:
            if generation != self._auth_generation:
                raise TidalAuthError("PKCE login was superseded")
            self._session = session
            self._last_user = payload
            self._save_session(session)
            return payload

    # -- persistence --------------------------------------------------------

    def _save_session(self, session: Any) -> None:
        data = {
            "token_type": session.token_type,
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "expiry_time": session.expiry_time.isoformat() if session.expiry_time else None,
            "is_pkce": bool(getattr(session, "is_pkce", False)),
            "user": self._last_user,
        }
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SESSION_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, SESSION_FILE)

    def _load_session(self) -> Any | None:
        if tidalapi is None or not SESSION_FILE.exists():
            return None
        try:
            data = json.loads(SESSION_FILE.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("Failed to read TIDAL session file: %s", exc)
            return None
        self._last_user = _parse_user_payload(data.get("user"))
        session = _new_session()
        try:
            ok = session.load_oauth_session(
                data.get("token_type") or "Bearer",
                data.get("access_token") or "",
                refresh_token=data.get("refresh_token"),
                expiry_time=_parse_expiry(data.get("expiry_time")),
                is_pkce=bool(data.get("is_pkce")),
            )
        except Exception as exc:  # noqa: BLE001
            # The user resolution inside load_oauth_session needs the network;
            # during an outage keep the loaded token state so the resilient
            # authenticated() check can still validate it once TIDAL returns,
            # and the cached library stays usable in the meantime.
            logger.warning("TIDAL session load incomplete (%s); keeping token state", exc)
            ok = bool(getattr(session, "access_token", None))
        return session if ok else None


def _parse_expiry(value: Any) -> Any:
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _parse_user_payload(value: Any) -> dict | None:
    """Read the persisted last-known user payload, tolerating legacy files."""
    if not isinstance(value, dict):
        return None
    user_id = value.get("id")
    return {
        "id": str(user_id) if user_id is not None else None,
        "email": str(value.get("email") or ""),
        "country_code": str(value.get("country_code") or "") or None,
    }


def _session_payload(session: Any) -> dict:
    user = getattr(session, "user", None)
    return {
        "authenticated": True,
        "user_id": getattr(user, "id", None) if user is not None else None,
        "email": getattr(user, "email", "") if user is not None else "",
        "country_code": getattr(session, "country_code", None),
        "is_pkce": bool(getattr(session, "is_pkce", False)),
    }


# Shared instance; the provider and API import this singleton.
manager = TidalSession()
