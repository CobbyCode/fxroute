# SPDX-License-Identifier: AGPL-3.0-only

"""Shared FastAPI HTTP error helpers.

Centralizes the recurring client-error mapping so controllers do not
re-declare ``HTTPException`` construction for every rejected value.  Modules
that must never import ``main`` (``dsp/api.py``, ``playback/queue.py``,
``measurement/*``) import these helpers directly.
"""

from __future__ import annotations

from fastapi import HTTPException


def bad_request(exc: Exception) -> HTTPException:
    """Return the HTTP 400 for a rejected client value."""
    return HTTPException(status_code=400, detail=str(exc))
