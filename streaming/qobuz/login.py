# SPDX-License-Identifier: AGPL-3.0-only

"""Qobuz/qbzd login orchestration (browser OAuth handoff).

qbzd owns the Qobuz OAuth flow itself: ``qbzd login`` prints the upstream
Qobuz authorization URL, runs a one-shot local redirect listener (or accepts
the pasted redirect URL via ``--paste``), stores the exchanged token in its
own credential file, and ``qbzd logout`` clears it.  This module only
orchestrates that CLI — no OAuth protocol logic of its own.

The FXRoute surface is:

* :func:`begin_login` — start ``qbzd login --paste``, read its banner and
  return the upstream authorization URL.
* :func:`finish_login` — pipe the operator-pasted redirect URL (or raw code)
  into the waiting process, then report qbzd's own verdict.
* :func:`logout` — run ``qbzd logout`` (credential reset; qbzd removes its
  ``.qbz-oauth-token`` file itself).

A single login runs at a time (module-level guard).  Every qbzd subprocess is
timeout-bounded and terminated on cancellation so no listener leaks.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from streaming.qobuz import backend  # noqa: F401  (re-exported context)

logger = logging.getLogger(__name__)

BANNER_TIMEOUT = 15.0
BANNER_SETTLE_SECONDS = 0.3
PROCESS_FINISH_TIMEOUT = 5.0
FINISH_TIMEOUT = 600.0
LOGOUT_TIMEOUT = 30.0

_URL_PATTERN = re.compile(r"https://\S+")
_CODE_PATTERN = re.compile(r"[?&]code=([A-Za-z0-9._~-]+)")


def qbzd_binary() -> str | None:
    """Resolve the qbzd binary the same way the backend probes installation."""
    return shutil.which("qbzd") or shutil.which("qbzd", path=str(Path.home() / ".local" / "bin"))


class _LoginSession:
    """One in-flight ``qbzd login --paste`` process."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.output: list[str] = []
        self.url: str | None = None


_login_lock = asyncio.Lock()
_session: _LoginSession | None = None


async def _read_until_url(proc: asyncio.subprocess.Process, timeout: float) -> tuple[str | None, list[str]]:
    """Read login output until the authorization URL appears (or EOF/timeout)."""
    collected: list[str] = []
    url: str | None = None

    async def _read() -> None:
        nonlocal url
        assert proc.stdout is not None
        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                return
            line = line_bytes.decode(errors="replace")
            collected.append(line)
            match = _URL_PATTERN.search(line)
            if match and url is None:
                url = match.group(0).rstrip(".,;)")

    try:
        await asyncio.wait_for(_read(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return url, collected


async def _terminate(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=PROCESS_FINISH_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def begin_login() -> dict[str, Any]:
    """Start the qbzd browser login and return its authorization URL."""
    global _session
    binary = qbzd_binary()
    if binary is None:
        raise RuntimeError("qbzd is not installed")
    async with _login_lock:
        if _session is not None:
            return {
                "started": False,
                "reason": "already-in-progress",
                "login_url": _session.url,
            }
        proc = await asyncio.create_subprocess_exec(
            binary, "login", "--paste",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE,
        )
        url, _collected = await _read_until_url(proc, BANNER_TIMEOUT)
        if not url:
            await _terminate(proc)
            raise RuntimeError("qbzd login did not report an authorization URL")
        # Let the listener settle into its blocking wait before returning.
        await asyncio.sleep(BANNER_SETTLE_SECONDS)
        session = _LoginSession(proc)
        session.url = url
        _session = session
        return {"started": True, "login_url": url}


async def finish_login(pasted: str) -> dict[str, Any]:
    """Feed the pasted redirect URL/code into the waiting qbzd login."""
    global _session
    value = str(pasted or "").strip()
    if not value:
        raise ValueError("Paste the redirect URL (or code) from the Qobuz sign-in page")
    async with _login_lock:
        session = _session
        if session is None or session.proc.returncode is not None:
            _session = None
            raise RuntimeError("No qbzd login is in progress; start it again")
        try:
            code_match = _CODE_PATTERN.search(value)
            payload = (code_match.group(1) if code_match else value) + "\n"
            assert session.proc.stdin is not None
            session.proc.stdin.write(payload.encode())
            await session.proc.stdin.drain()
            session.proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, AssertionError) as exc:
            await _terminate(session.proc)
            _session = None
            raise RuntimeError("qbzd login exited before the code arrived") from exc
        try:
            rest = await asyncio.wait_for(_drain_remaining(session.proc), timeout=FINISH_TIMEOUT)
        except asyncio.TimeoutError:
            await _terminate(session.proc)
            _session = None
            raise RuntimeError("qbzd login timed out waiting for the browser step") from None
        finally:
            _session = None
        output = "".join(rest)
        ok = session.proc.returncode == 0
        return {
            "ok": ok,
            "returncode": session.proc.returncode,
            "output": output[-1500:],
        }


async def _drain_remaining(proc: asyncio.subprocess.Process) -> list[str]:
    """Read the process to EOF after stdin was closed."""
    collected: list[str] = []
    assert proc.stdout is not None
    while True:
        line_bytes = await proc.stdout.readline()
        if not line_bytes:
            break
        collected.append(line_bytes.decode(errors="replace"))
    await proc.wait()
    return collected


async def state() -> dict[str, Any]:
    """Return whether a qbzd browser login is currently in flight."""
    async with _login_lock:
        session = _session
        if session is None:
            return {"in_progress": False}
        alive = session.proc.returncode is None
        return {
            "in_progress": alive,
            "login_url": session.url,
            "expired": not alive,
        }


async def cancel() -> dict[str, Any]:
    """Terminate any in-flight qbzd login (browser dialog closed/aborted)."""
    global _session
    async with _login_lock:
        session = _session
        if session is None:
            return {"cancelled": False, "in_progress": False}
        await _terminate(session.proc)
        _session = None
        return {"cancelled": True, "in_progress": False}


async def logout() -> dict[str, Any]:
    """Run ``qbzd logout`` (qbzd clears its own credential file)."""
    global _session
    binary = qbzd_binary()
    if binary is None:
        raise RuntimeError("qbzd is not installed")
    async with _login_lock:
        if _session is not None:
            await _terminate(_session.proc)
            _session = None
        proc = await asyncio.create_subprocess_exec(
            binary, "logout",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=LOGOUT_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("qbzd logout timed out") from None
        output = (stdout or b"").decode(errors="replace")
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": output[-1000:]}
