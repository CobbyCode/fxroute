# SPDX-License-Identifier: AGPL-3.0-only
"""Known rate-dependent interfaces and their ALSA USB playback inventories."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

_MODEL = re.compile(r"Focusrite[ _]Scarlett[ _](16i16|18i16|18i20)[ _]4th[ _]Gen(?:[ _.-]|$)", re.I)
_RATES = {44100, 48000, 88200, 96000, 176400, 192000}


def profile_id(identity: str) -> str | None:
    match = _MODEL.search(identity)
    return f"scarlett-{match[1].lower()}-4th-gen" if match else None


def playback_tiers(text: str) -> list[dict[str, Any]]:
    """Read USB playback channel/rate pairs, excluding capture and status."""
    if "Playback:" not in text:
        return []
    playback = text.split("Playback:", 1)[1].split("Capture:", 1)[0]
    by_channels: dict[int, set[int]] = {}
    for block in re.split(r"(?m)^  Interface \d+\s*$", playback)[1:]:
        channels = re.search(r"(?m)^\s+Channels:\s*(\d+)", block)
        rates = re.search(r"(?m)^\s+Rates:\s*([\d, ]+)", block)
        if not channels or not rates or "Format: DSD" in block:
            continue
        count = int(channels[1])
        values = {int(rate.strip()) for rate in rates[1].split(",") if rate.strip()} & _RATES
        if count > 2 and values:
            by_channels.setdefault(count, set()).update(values)
    if len(by_channels) < 2:
        return []
    return [{"id": f"{count}ch", "channels": count, "rates": sorted(rates), "probe_rate": max(rates)}
            for count, rates in sorted(by_channels.items(), reverse=True)]


def cumulative_rates(tiers: Sequence[Mapping], active_id: str | None) -> list[int]:
    """All selectable rates of one tier, including every lower band.

    A smaller channel inventory never takes rates away: the 14-channel tier
    still offers 44.1/48 kHz, the 10-channel tier everything below it.
    """
    bands = [tier for tier in tiers if isinstance(tier, Mapping)]
    active = next((tier for tier in bands if tier.get("id") == active_id), None)
    if active is None:
        return []
    ceiling = max(int(tier.get("channels") or 0) for tier in bands)
    floor = int(active.get("channels") or 0)
    rates: set[int] = set()
    for tier in bands:
        channels = int(tier.get("channels") or 0)
        if floor <= channels <= ceiling:
            rates.update(int(rate) for rate in tier.get("rates") or [])
    return sorted(rates)


def native_tier_for_rate(tiers: Sequence[Mapping], rate: int | None) -> dict | None:
    """The tier whose native band carries ``rate``, if any.

    Native bands are disjoint per device, so this identifies at most one
    tier: the only inventory on which the rate runs without resampling.
    """
    for tier in tiers:
        if isinstance(tier, Mapping) and rate in (tier.get("rates") or []):
            return dict(tier)
    return None


def device_rates(tiers: Sequence[Mapping]) -> list[int]:
    """Every rate the profiled device offers across all tiers."""
    rates: set[int] = set()
    for tier in tiers:
        if isinstance(tier, Mapping):
            rates.update(int(rate) for rate in tier.get("rates") or [])
    return sorted(rates)


def required_tier_switch(selected_output: Mapping, target_rate: int | None) -> dict | None:
    """The tier switch a rate needs, or None when the active tier carries it.

    Returns ``{"key", "tier", "rates"}`` with the destination tier's native
    rates when ``target_rate`` runs natively on another tier, otherwise None
    (same tier, unknown profile, or unrepresentable rate).
    """
    profile = selected_output.get("device_profile") or {}
    tiers = [tier for tier in profile.get("tiers") or [] if isinstance(tier, dict)]
    if not tiers or not isinstance(target_rate, int) or target_rate <= 0:
        return None
    destination = native_tier_for_rate(tiers, target_rate)
    if destination is None or destination.get("id") == profile.get("active_tier"):
        return None
    return {
        "key": selected_output.get("key"),
        "tier": destination,
        "rates": list(destination.get("rates") or []),
    }


def rate_in_tier(rate: int | None, rates: Sequence[int]) -> int:
    """Keep the rate family where possible; an unknown source uses 48 kHz family."""
    if not rates:
        raise ValueError("Channel tier has no supported sample rates")
    if rate in rates:
        return int(rate)
    family = 44100 if rate and rate % 44100 == 0 else 48000
    candidates = [value for value in rates if value % family == 0] or list(rates)
    return min(candidates, key=lambda value: abs(value - (rate or 48000)))


def discover_profile(output_key: str, details: dict, channels: int | None) -> dict | None:
    profile = profile_id(output_key)
    card = str(details.get("alsa_card") or "")
    if profile is None or not card.isdigit():
        return None
    texts = []
    for path in Path(f"/proc/asound/card{card}").glob("stream*"):
        try:
            texts.append(path.read_text())
        except OSError:
            continue
    tiers = playback_tiers("\n".join(texts))
    if not tiers:
        return None
    active = next((tier for tier in tiers if tier["channels"] == channels), None)
    return {"id": profile, "tiers": tiers, "active_tier": active["id"] if active else None,
            "source": "alsa-usb-playback", "manual": True}
