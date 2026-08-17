# SPDX-License-Identifier: AGPL-3.0-only

"""TIDAL OAuth/device-flow session management.

Owns the ``tidalapi`` session lifecycle: creating a session, running the
headless device-authorization flow (or the browser PKCE flow), persisting the
OAuth session to the FXRoute config directory and reloading/refreshing it.

FXRoute never sees the TIDAL password: both flows hand the user a URL (and, for
the device flow, a short code) and only the resulting OAuth tokens are stored.
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
from dataclasses import dataclass
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


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "fxroute"


SESSION_FILE = _config_dir() / "tidal-session.json"

# Default requested stream quality. TIDAL downgrades gracefully: HI_RES_LOSSLESS
# yields 24-bit FLAC where available and 16-bit FLAC otherwise (both require the
# PKCE login). The link/device-authorization flow is capped at HIGH (320k AAC)
# by TIDAL regardless of the requested quality.
DEFAULT_QUALITY = "HI_RES_LOSSLESS"
DEVICE_LOGIN_QUALITY = "HIGH"


@dataclass
class DeviceLogin:
    """A started device-authorization flow, pending user approval."""

    verification_uri: str
    verification_uri_complete: str
    user_code: str
    expires_in: int


def _new_session(quality: str | None = None) -> "tidalapi.Session":
    if tidalapi is None:
        raise TidalAuthError("tidalapi is not installed")
    session = tidalapi.Session()
    session.config.quality = quality or DEFAULT_QUALITY
    return session


class TidalSession:
    """Holds the active tidalapi session and the pending login state.

    A single instance is shared by the provider; the application is
    single-user, so one session (and one pending device login at a time) is
    enough. Auth mutations run under an asyncio lock so a device-login start
    and finish never race.
    """

    def __init__(self) -> None:
        self._session: Any | None = None
        self._pending: Any | None = None  # LinkLogin for the device flow
        self._pending_session: Any | None = None
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
        s = self.session()
        return bool(s is not None and s.check_login())

    async def is_authenticated(self) -> bool:
        """Offload the (network-backed) login check off the event loop."""
        return await asyncio.to_thread(self.authenticated)

    async def clear(self) -> None:
        """Forget the in-memory session and remove the persisted token file."""
        async with self._lock:
            self._session = None
            self._pending = None
            self._pending_session = None
            try:
                if SESSION_FILE.exists():
                    SESSION_FILE.unlink()
            except OSError as exc:
                logger.warning("Failed to remove TIDAL session file: %s", exc)

    # -- device login -------------------------------------------------------

    def start_device_login(self, quality: str | None = None) -> DeviceLogin:
        """Start the headless device-authorization flow.

        Returns the URL + user code the user must enter at ``link.tidal.com``;
        call :meth:`finish_device_login` after the user confirms.  The device
        flow is TIDAL-capped at HIGH (320k AAC); use the PKCE flow for
        lossless/Hi-Res.
        """
        if tidalapi is None:
            raise TidalAuthError("tidalapi is not installed")
        session = _new_session(quality or DEVICE_LOGIN_QUALITY)
        link = session.get_link_login()
        self._pending = link
        self._pending_session = session
        return DeviceLogin(
            verification_uri=link.verification_uri,
            verification_uri_complete=link.verification_uri_complete,
            user_code=link.user_code,
            expires_in=int(link.expires_in),
        )

    def finish_device_login(self) -> dict:
        """Wait for the user to approve the device login (blocking).

        ``process_link_login(until_expiry=True)`` blocks on a worker thread
        until approval or expiry, so callers must run it via
        ``asyncio.to_thread``.  Returns a normalized
        ``{authenticated, user_id, email, country_code, is_pkce}``.
        """
        if self._pending is None or self._pending_session is None:
            raise TidalAuthError("no device login is in progress")
        session = self._pending_session
        try:
            session.process_link_login(self._pending, until_expiry=True)
        except Exception as exc:  # noqa: BLE001 - tidalapi raises broad types
            raise TidalAuthError(f"device login failed: {exc}") from exc
        finally:
            self._pending = None
            self._pending_session = None
        if not session.check_login():
            raise TidalAuthError("device login expired before authorization")
        self._session = session
        self._save_session(session)
        return _session_payload(session)

    async def start_device_login_async(self, quality: str | None = None) -> DeviceLogin:
        async with self._lock:
            return await asyncio.to_thread(self.start_device_login, quality)

    async def finish_device_login_async(self) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self.finish_device_login)

    # -- PKCE login (browser; required for Hi-Res 24-bit FLAC) --------------

    def pkce_login_url(self) -> str:
        """Return the browser login URL for the PKCE flow.

        The user opens it, signs in, and is redirected to an "Oops" page whose
        full URL must be pasted back via :meth:`finish_pkce_login`.
        """
        if tidalapi is None:
            raise TidalAuthError("tidalapi is not installed")
        session = _new_session(DEFAULT_QUALITY)
        self._pending_session = session
        return session.pkce_login_url()

    def finish_pkce_login(self, redirect_url: str) -> dict:
        """Exchange the pasted PKCE redirect URL for an authenticated session.

        The pasted URL is the TIDAL "Oops" redirect page the browser lands on;
        ``pkce_get_auth_token`` extracts its ``code`` and exchanges it for
        tokens, which ``process_auth_token`` then applies to the session.
        """
        if self._pending_session is None:
            raise TidalAuthError("no PKCE login is in progress")
        session = self._pending_session
        self._pending_session = None
        try:
            token = session.pkce_get_auth_token(redirect_url)
            session.process_auth_token(token, is_pkce_token=True)
        except Exception as exc:  # noqa: BLE001
            raise TidalAuthError(f"PKCE login failed: {exc}") from exc
        if not session.check_login():
            raise TidalAuthError("PKCE login did not produce a valid session")
        self._session = session
        self._save_session(session)
        return _session_payload(session)

    async def pkce_login_url_async(self) -> str:
        async with self._lock:
            return await asyncio.to_thread(self.pkce_login_url)

    async def finish_pkce_login_async(self, redirect_url: str) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self.finish_pkce_login, redirect_url)

    # -- persistence --------------------------------------------------------

    def _save_session(self, session: Any) -> None:
        data = {
            "token_type": session.token_type,
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "expiry_time": session.expiry_time.isoformat() if session.expiry_time else None,
            "is_pkce": bool(getattr(session, "is_pkce", False)),
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
            logger.warning("Failed to load TIDAL session: %s", exc)
            return None
        return session if ok and session.check_login() else None


def _parse_expiry(value: Any) -> Any:
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


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
