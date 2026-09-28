# SPDX-License-Identifier: AGPL-3.0-only
"""Selected-output rate capability and the playback rate fallback.

A source rate the selected output supports plays natively.  A rate it does
not support is resampled (by PipeWire) to the highest supported rate below
it, e.g. 96 kHz -> 48 kHz on a 48 kHz-only card or 384 kHz -> 192 kHz on a
192 kHz device.  When no supported rate is lower, the lowest supported rate
is used.  An unknown capability never changes a rate.

The capability of the selected output is remembered from every output
overview read (the overview is the only place that enumerates it), so rate
decisions outside a transition snapshot use the same list.
"""

from __future__ import annotations

from typing import Iterable

_selected_output_rates: tuple[int, ...] = ()


def remember_selected_output_rates(rates: Iterable[int] | None) -> None:
    """Record the selected output's FXRoute-processable rates (empty: unknown)."""
    global _selected_output_rates
    _selected_output_rates = tuple(
        sorted({rate for rate in (rates or []) if isinstance(rate, int) and rate > 0})
    )


def selected_output_rates() -> tuple[int, ...]:
    """Return the last known selected-output rates (empty when unknown)."""
    return _selected_output_rates


def playable_rate(rate: int | None, supported_rates: Iterable[int] | None = None) -> int | None:
    """Return the rate the selected output plays for ``rate``.

    ``supported_rates`` defaults to the remembered selected-output
    capability.  Native when supported, else the highest supported rate
    below ``rate``, else the lowest supported rate.
    """
    if not isinstance(rate, int) or rate <= 0:
        return rate
    if supported_rates is None:
        supported = _selected_output_rates
    else:
        supported = tuple(sorted({item for item in supported_rates if isinstance(item, int) and item > 0}))
    if not supported or rate in supported:
        return rate
    lower = [item for item in supported if item < rate]
    return lower[-1] if lower else supported[0]
