# SPDX-License-Identifier: AGPL-3.0-only

"""qbzd control-plane client.

qbzd exposes a plain HTTP control plane on its daemon port (default
``127.0.0.1:8182``). This module is the only place that speaks that protocol;
the provider normalizes the JSON into the shared streaming models and nothing
qbzd-specific crosses the provider boundary.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8182
DEFAULT_TIMEOUT = 2.0
PING_TIMEOUT = 1.0


def qbzd_installed() -> bool:
    return shutil.which("qbzd") is not None


def default_base_url() -> str:
    return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def _get(base_url: str, path: str, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    try:
        resp = requests.get(base_url + path, timeout=timeout)
    except requests.RequestException as exc:
        logger.debug("qbzd GET %s failed: %s", path, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _post(base_url: str, path: str, body: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    try:
        resp = requests.post(base_url + path, json=body or {}, timeout=timeout)
    except requests.RequestException as exc:
        logger.debug("qbzd POST %s failed: %s", path, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return {}


async def get_json(base_url: str, path: str, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    return await asyncio.to_thread(_get, base_url, path, timeout)


async def post_json(base_url: str, path: str, body: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    return await asyncio.to_thread(_post, base_url, path, body, timeout)


async def is_reachable(base_url: str, timeout: float = PING_TIMEOUT) -> bool:
    """Return whether the qbzd daemon answers on its control plane.

    ``/api/status`` always returns 200 JSON while the daemon runs (including
    the not-yet-authenticated state), so it is the honest liveness probe.
    """
    return (await get_json(base_url, "/api/status", timeout)) is not None
