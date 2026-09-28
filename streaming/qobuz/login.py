# SPDX-License-Identifier: AGPL-3.0-only

"""Qobuz/qbzd login orchestration (browser OAuth handoff via the daemon HTTP API).

The fork daemon owns the Qobuz OAuth flow itself: ``POST
/api/auth/oauth/start`` returns the upstream authorization URL, the daemon
serves the OAuth callback on its own port, ``GET /api/auth/oauth/status``
reports the pending login, and the exchanged token is stored by the daemon
(keyring with a file fallback). This module only orchestrates that HTTP
flow — no OAuth protocol logic of its own.

The FXRoute surface is unchanged:

* :func:`begin_login` — start the OAuth flow and return its sign-in URL.
* :func:`finish_login` — hand the operator-pasted redirect URL (or raw
  code) to the daemon callback, then report the daemon verdict.
* :func:`logout` — delete the daemon credential artifacts and restart the
  daemon so the running session actually ends.
* :func:`state` / :func:`cancel` — in-flight session bookkeeping.

A single login runs at a time (module-level guard). All daemon calls are
timeout-bounded.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import urllib.parse
from pathlib import Path
from typing import Any

from streaming.qobuz import backend

logger = logging.getLogger(__name__)

OAUTH_START_TIMEOUT = 15.0
OAUTH_CALLBACK_TIMEOUT = 15.0
OAUTH_STATUS_INTERVAL = 2.0
OAUTH_STATUS_TIMEOUT = 300.0
FINISH_TIMEOUT = 330.0
RESTART_TIMEOUT = 20.0
POST_LOGIN_VERIFY_TIMEOUT = 15.0

_CODE_PATTERN = re.compile(r"[?&]code=([A-Za-z0-9._~-]+)")


def qbzd_binary() -> str | None:
    """Resolve the qbzd binary the same way the backend probes installation."""
    return shutil.which("qbzd") or shutil.which("qbzd", path=str(Path.home() / ".local" / "bin"))


def _base_url() -> str:
    return backend.default_base_url()


def _http_post(path: str, body: dict | None, timeout: float) -> dict | None:
    import requests

    try:
        resp = requests.post(_base_url() + path, json=body or {}, timeout=timeout)
    except requests.RequestException as exc:
        logger.debug("qbzd POST %s failed: %s", path, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else {}


def _http_get(path: str, timeout: float) -> dict | str | None:
    import requests

    try:
        resp = requests.get(_base_url() + path, timeout=timeout)
    except requests.RequestException as exc:
        logger.debug("qbzd GET %s failed: %s", path, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


class _LoginSession:
    """One in-flight daemon-side OAuth flow."""

    def __init__(self, url: str, callback_url: str | None) -> None:
        self.url = url
        self.callback_url = callback_url


_login_lock = asyncio.Lock()
_session: _LoginSession | None = None


async def _daemon_oauth_status() -> str | None:
    """Return the daemon OAuth status (idle/pending/success/error), if known."""
    data = await asyncio.to_thread(_http_get, "/api/auth/oauth/status", OAUTH_CALLBACK_TIMEOUT)
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    return str(status) if status else None


async def begin_login() -> dict[str, Any]:
    """Start the qbzd browser login and return its authorization URL."""
    global _session
    if qbzd_binary() is None:
        raise RuntimeError("qbzd is not installed")
    async with _login_lock:
        if _session is not None:
            daemon_status = await _daemon_oauth_status()
            if daemon_status == "pending":
                return {
                    "started": False,
                    "reason": "already-in-progress",
                    "login_url": _session.url,
                }
            _session = None
        # No callback_host: the daemon auto-detects its LAN IP so the
        # browser redirect reaches the daemon callback from any host on
        # the LAN.
        data = await asyncio.to_thread(_http_post, "/api/auth/oauth/start", None, OAUTH_START_TIMEOUT)
        if not data or not data.get("oauth_url"):
            probe = await asyncio.to_thread(_http_get, "/api/status", OAUTH_CALLBACK_TIMEOUT)
            if isinstance(probe, dict):
                raise RuntimeError(
                    "qbzd does not offer browser login; update the Qobuz provider to the current build"
                )
            raise RuntimeError("qbzd login did not report an authorization URL")
        url = str(data["oauth_url"])
        callback_url = data.get("callback_url")
        _session = _LoginSession(url, str(callback_url) if callback_url else None)
        return {"started": True, "login_url": url}


def _extract_code(pasted: str) -> str:
    """Return the OAuth code from a pasted redirect URL (or raw code)."""
    value = pasted.strip()
    match = _CODE_PATTERN.search(value)
    if match:
        return match.group(1)
    parsed = urllib.parse.urlparse(value)
    if parsed.query:
        params = urllib.parse.parse_qs(parsed.query)
        for key in ("code", "code_autorisation"):
            if params.get(key):
                return params[key][0]
    return value


async def finish_login(pasted: str) -> dict[str, Any]:
    """Complete the OAuth flow with the pasted redirect URL/code."""
    global _session
    value = str(pasted or "").strip()
    if not value:
        raise ValueError("Paste the redirect URL (or code) from the Qobuz sign-in page")
    async with _login_lock:
        session = _session
        if session is None:
            daemon_status = await _daemon_oauth_status()
            if daemon_status == "success":
                return {"ok": True, "status": "success"}
            raise RuntimeError("No qbzd login is in progress; start it again")
        code = _extract_code(value)
        if not code:
            raise ValueError("Paste the redirect URL (or code) from the Qobuz sign-in page")
        callback = await asyncio.to_thread(
            _http_get, f"/api/auth/oauth/callback?code={urllib.parse.quote(code)}", OAUTH_CALLBACK_TIMEOUT
        )
        # The callback answers an HTML page (or an error); the verdict
        # comes from the OAuth status poll below.
        _ = callback
        try:
            async with asyncio.timeout(FINISH_TIMEOUT):
                while True:
                    daemon_status = await _daemon_oauth_status()
                    if daemon_status == "success":
                        return {"ok": True, "status": "success"}
                    if daemon_status == "error":
                        return {"ok": False, "status": "error"}
                    if daemon_status == "idle":
                        # The daemon dropped the flow (restart/timeout):
                        # report instead of polling a dead flow.
                        return {"ok": False, "status": "idle"}
                    await asyncio.sleep(OAUTH_STATUS_INTERVAL)
        except TimeoutError:
            raise RuntimeError("qbzd login timed out waiting for the browser step") from None
        finally:
            _session = None


async def state() -> dict[str, Any]:
    """Return whether a qbzd browser login is currently in flight."""
    global _session
    async with _login_lock:
        session = _session
        if session is None:
            return {"in_progress": False}
        daemon_status = await _daemon_oauth_status()
        if daemon_status == "pending":
            return {"in_progress": True, "login_url": session.url, "expired": False}
        _session = None
        return {"in_progress": False, "login_url": session.url, "expired": True}


async def cancel() -> dict[str, Any]:
    """Drop the in-flight login session (the daemon flow expires on its own)."""
    global _session
    async with _login_lock:
        if _session is None:
            return {"cancelled": False, "in_progress": False}
        _session = None
        return {"cancelled": True, "in_progress": False}


def _credential_paths() -> list[Path]:
    """Daemon credential artifacts (file fallback; keyring is headless-absent)."""
    data_root = Path.home() / ".local" / "share" / "qbz"
    return [
        data_root / ".oauth-token",
        data_root / "last_user_id",
        data_root / "users",
    ]


async def _restart_daemon() -> None:
    """Restart the qbzd user service; raise on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "restart",
            "qbzd.service",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=RESTART_TIMEOUT)
    except (OSError, asyncio.TimeoutError) as exc:
        raise RuntimeError("qbzd restart timed out") from exc
    if proc.returncode != 0:
        raise RuntimeError("qbzd restart failed")


async def logout() -> dict[str, Any]:
    """Clear the daemon credential and restart it so the session ends."""
    global _session
    if qbzd_binary() is None:
        raise RuntimeError("qbzd is not installed")
    async with _login_lock:
        _session = None
        removed: list[str] = []
        for path in _credential_paths():
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=False)
                    removed.append(path.name)
                elif path.is_file() or path.is_symlink():
                    path.unlink()
                    removed.append(path.name)
            except OSError as exc:
                logger.debug("qbzd credential cleanup skipped %s: %s", path, exc)
        await _restart_daemon()
        try:
            async with asyncio.timeout(POST_LOGIN_VERIFY_TIMEOUT):
                while True:
                    data = await asyncio.to_thread(_http_get, "/api/status", OAUTH_CALLBACK_TIMEOUT)
                    if isinstance(data, dict):
                        return {
                            "ok": data.get("logged_in") is not True,
                            "logged_in": bool(data.get("logged_in")),
                            "removed": removed,
                        }
                    await asyncio.sleep(1.0)
        except TimeoutError:
            raise RuntimeError("qbzd did not come back after logout") from None
