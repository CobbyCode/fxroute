# SPDX-License-Identifier: AGPL-3.0-only

"""Shared FastAPI HTTP error helpers.

Centralizes the recurring client-error mapping so controllers do not
re-declare ``HTTPException`` construction for every rejected value.  Modules
that must never import ``main`` (``dsp/api.py``, ``playback/queue.py``,
``measurement/*``) import these helpers directly.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

logger = logging.getLogger(__name__)


def bad_request(exc: Exception) -> HTTPException:
    """Return the HTTP 400 for a rejected client value."""
    return HTTPException(status_code=400, detail=str(exc))


def internal_error(context: str, exc: Exception) -> HTTPException:
    """Log the full exception server-side and return a generic HTTP 500.

    The exception text may contain internal paths/state; it stays in the
    log, never in the client response.
    """
    logger.exception("%s", context)
    return HTTPException(status_code=500, detail="Internal server error")
