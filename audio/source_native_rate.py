# SPDX-License-Identifier: AGPL-3.0-only
"""Native sample rates of the active audio source (auto policy).

Single home for the "actual stream rate handed to FXRoute" per source mode.
App-playback sources (MPV/Spotify/Qobuz) keep their existing resolution in
``playback.orchestration``; this module owns the line sources whose rates the
auto policy previously ignored (Bluetooth A2DP node rate, STDIN session spec,
digital external helper rate). Analog external inputs have no native stream
rate (the converter follows the graph), so they resolve to None: keep the
graph, no SRC.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from audio.samplerate.constants import SAMPLE_RATE_CANDIDATES

_VALID_RATES = frozenset(SAMPLE_RATE_CANDIDATES)

_DIGITAL_HINTS = ("iec958", "spdif", "s/pdif", "optical", "coax", "toslink", "digital")


def _valid_rate(value: Any) -> int | None:
    """Return value when it is a supported candidate rate, else None."""
    if type(value) is bool or not isinstance(value, int):
        return None
    return value if value in _VALID_RATES else None


def _rate_from_fraction(text: Any, *, denominator: bool = False) -> int | None:
    """Parse a PipeWire ``numerator/denominator`` rate field.

    ``node.rate`` is ``1/48000`` (rate is the denominator);
    ``node.latency`` is ``512/48000`` (rate is also the denominator).
    """
    match = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", str(text or ""))
    if not match:
        return None
    return _valid_rate(int(match.group(2 if denominator else 1)))


def _node_matches(props: Mapping[str, Any], node_name: str) -> bool:
    return str(props.get("node.name") or "").strip() == node_name


def parse_bluetooth_node_rate(pw_dump_text: str, node_name: str) -> int | None:
    """Return the live A2DP source rate of a Bluetooth node, else None.

    Priority: negotiated ``Format`` rate, ``node.rate``, ``node.latency``
    denominator, single-valued ``EnumFormat``. Only rates from
    ``SAMPLE_RATE_CANDIDATES`` count; anything else (or an unparseable dump)
    is None so the caller keeps the graph instead of switching to garbage.
    """
    target = (node_name or "").strip()
    if not target:
        return None
    try:
        objects = json.loads(pw_dump_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(objects, list):
        return None
    for entry in objects:
        if not isinstance(entry, dict):
            continue
        info = entry.get("info")
        if not isinstance(info, dict):
            continue
        props = info.get("props")
        if not isinstance(props, dict) or not _node_matches(props, target):
            continue
        params = info.get("params")
        params = params if isinstance(params, dict) else {}
        for block in params.get("Format") or []:
            rate = _valid_rate(block.get("rate") if isinstance(block, dict) else None)
            if rate is not None:
                return rate
        rate = _rate_from_fraction(props.get("node.rate"), denominator=True)
        if rate is not None:
            return rate
        rate = _rate_from_fraction(props.get("node.latency"), denominator=True)
        if rate is not None:
            return rate
        enum_rates = set()
        for block in params.get("EnumFormat") or []:
            rate = _valid_rate(block.get("rate") if isinstance(block, dict) else None)
            if rate is not None:
                enum_rates.add(rate)
        if len(enum_rates) == 1:
            return next(iter(enum_rates))
        return None
    return None


def _bluetooth_session_name(overview: Mapping[str, Any] | None) -> str | None:
    if not isinstance(overview, Mapping):
        return None
    bluetooth = overview.get("bluetooth")
    if isinstance(bluetooth, Mapping):
        name = str(bluetooth.get("source_name") or "").strip()
        if name:
            return name
    session = overview.get("receiver_session")
    if isinstance(session, Mapping):
        name = str(session.get("source_name") or "").strip()
        if name:
            return name
    return None


def bluetooth_source_rate(*, overview: Mapping[str, Any] | None = None,
                          pw_dump: str | None = None,
                          node_name: str | None = None) -> int | None:
    """Return the live Bluetooth A2DP rate, else None."""
    target = (node_name or "").strip() or _bluetooth_session_name(overview)
    if not target or not isinstance(pw_dump, str) or not pw_dump:
        return None
    return parse_bluetooth_node_rate(pw_dump, target)


def stdin_source_rate(snapshot: Mapping[str, Any] | None = None) -> int | None:
    """Return the STDIN session spec rate while a session is active."""
    if not isinstance(snapshot, Mapping):
        return None
    if not snapshot.get("session_id"):
        return None
    return _valid_rate(snapshot.get("rate"))


def _entry_text(entry: Mapping[str, Any]) -> str:
    return " ".join(
        str(entry.get(name) or "")
        for name in ("source_key", "name", "port_key", "port_label",
                     "device_label", "pair_label")
    ).lower()


def is_digital_external_entry(entry: Mapping[str, Any] | None) -> bool:
    """Return whether an external-input entry carries a digital stream."""
    if not isinstance(entry, Mapping):
        return False
    return any(hint in _entry_text(entry) for hint in _DIGITAL_HINTS)


def external_source_rate(entry: Mapping[str, Any] | None,
                         helper_sample_rate: int | None = None) -> int | None:
    """Return the native rate of an external input, else None.

    Analog inputs have no stream rate of their own: the converter runs at the
    graph rate, so keeping the graph is native and SRC-free. Digital inputs
    follow the helper-decoded stream rate when it is readable.
    """
    if not is_digital_external_entry(entry):
        return None
    return _valid_rate(helper_sample_rate)


def resolve_active_source_native_rate(
    mode: str | None, *,
    bluetooth_overview: Mapping[str, Any] | None = None,
    pw_dump: str | None = None,
    stdin_snapshot: Mapping[str, Any] | None = None,
    external_entry: Mapping[str, Any] | None = None,
    helper_sample_rate: int | None = None,
) -> int | None:
    """Dispatch the native-rate lookup by committed source mode."""
    normalized = (mode or "").strip()
    if normalized == "bluetooth-input":
        return bluetooth_source_rate(overview=bluetooth_overview, pw_dump=pw_dump)
    if normalized == "stdin-input":
        return stdin_source_rate(stdin_snapshot)
    if normalized == "external-input":
        return external_source_rate(external_entry, helper_sample_rate)
    return None
