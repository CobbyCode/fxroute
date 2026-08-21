# SPDX-License-Identifier: AGPL-3.0-only

"""FXRoute-is-the-active-Qobuz-Connect-renderer tracking.

qbzd's HTTP API does not expose whether this device is the actively selected
renderer: ``session_active`` persists across device switches and the last
track stays paused with its metadata after deselection. The journal watch
therefore maintains this flag from ``SET_ACTIVE`` renderer commands.

``None`` means no evidence yet (fresh start without journal history);
consumers must treat ``None`` conservatively (not standby).
"""

from __future__ import annotations

_device_active: bool | None = None


def set_device_active(value: bool) -> None:
    global _device_active
    _device_active = bool(value)


def is_device_active() -> bool | None:
    """Return the last observed selection state, or ``None`` if unknown."""
    return _device_active


def reset() -> None:
    global _device_active
    _device_active = None
