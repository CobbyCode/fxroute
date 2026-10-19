# SPDX-License-Identifier: AGPL-3.0-only
"""HDMI transport profiles: receiver ELD, card profiles and channel positions.

ACP publishes every HDMI connector of a card under three output mappings,
``hdmi-stereo``, ``hdmi-surround`` (5.1) and ``hdmi-surround71`` (7.1), with an
``-extraN`` suffix for connector N > 0. The sink name therefore changes with
the transport while the physical connector stays the same: the connector
identity is the ALSA card plus N. Which transports a connector can carry comes
from the receiver's ELD (its LPCM short audio descriptors); the card profile
list only says which mappings exist.

Live links follow the sink's real PipeWire ports. The routing editor uses
those ports when they cover capacity, otherwise the standard ACP positions.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from common.atomic_write import atomic_write_text
from audio.output_ports import order_hardware_playback_ports

TRANSPORTS = (
    {"id": "stereo", "label": "Stereo", "channels": 2, "mapping": "hdmi-stereo"},
    {"id": "5.1", "label": "5.1", "channels": 6, "mapping": "hdmi-surround"},
    {"id": "7.1", "label": "7.1", "channels": 8, "mapping": "hdmi-surround71"},
)
_BY_ID = {transport["id"]: transport for transport in TRANSPORTS}
_BY_MAPPING = {transport["mapping"]: transport for transport in TRANSPORTS}
_SINK = re.compile(r"^alsa_output\.(?P<device>.+)\.(?P<mapping>hdmi-(?:stereo|surround71|surround))"
                   r"(?:-extra(?P<extra>\d+))?$")
_MAPPING = re.compile(r"^(?P<mapping>hdmi-(?:stereo|surround71|surround))(?:-extra(?P<extra>\d+))?$")

# CEA-861 short audio descriptor rate bits, in bit order.
_SAD_RATES = (32000, 44100, 48000, 88200, 96000, 176400, 192000)
_SAD_FORMAT_LPCM = 1
_ELD_FIXED_BYTES = 20
_ELD_MAX_MNL = 16

_POSITION_LABELS = {"FL": "Front L", "FR": "Front R", "FC": "Center", "LFE": "LFE",
                    "RL": "Rear L", "RR": "Rear R", "SL": "Side L", "SR": "Side R"}
# One role per receiver channel. HDMI carries a single LFE channel, so the
# defaults route one sub (Sub 1) and never a second independent sub.
_DEFAULT_ROLES = {"FL": "main_l", "FR": "main_r", "FC": "center", "LFE": "sub1",
                  "RL": "rear_l", "RR": "rear_r", "SL": "side_l", "SR": "side_r"}
_MODE_ROLES = {"stereo": {"main_l", "main_r"}, "stereo-sub": {"main_l", "main_r", "sub1"},
               "surround": set(_DEFAULT_ROLES.values())}

RESTORE_RETRY_SECONDS = 30.0


@dataclass(frozen=True)
class HdmiSink:
    """One HDMI connector of a card, played through one transport."""

    card: str
    connector: int
    transport: str

    @property
    def identity(self) -> str:
        """Stable connector identity across transport (sink name) changes."""
        return f"{self.card}#hdmi{self.connector}"


def transport(transport_id: str) -> dict:
    try:
        return dict(_BY_ID[transport_id])
    except KeyError:
        raise ValueError(f"Unknown HDMI transport: {transport_id}") from None


def parse_sink_key(key: object) -> HdmiSink | None:
    """The HDMI connector and transport behind an ACP HDMI sink name."""
    match = _SINK.match(str(key or "").strip())
    if not match:
        return None
    return HdmiSink(f"alsa_card.{match['device']}", int(match["extra"] or 0),
                    _BY_MAPPING[match["mapping"]]["id"])


def is_hdmi_key(key: object) -> bool:
    return parse_sink_key(key) is not None


def mapping_name(connector: int, transport_id: str) -> str:
    base = transport(transport_id)["mapping"]
    return f"{base}-extra{connector}" if connector else base


def sink_key(card: str, connector: int, transport_id: str) -> str:
    device = str(card).removeprefix("alsa_card.")
    return f"alsa_output.{device}.{mapping_name(connector, transport_id)}"


def split_card_profile(profile: object) -> tuple[str | None, str | None]:
    """(output mapping, input mapping) of an ACP profile such as
    ``output:hdmi-stereo+input:analog-stereo``."""
    output = input_ = None
    for part in str(profile or "").split("+"):
        kind, _, mapping = part.partition(":")
        if kind == "output" and mapping:
            output = mapping
        elif kind == "input" and mapping:
            input_ = mapping
    return output, input_


def card_profile_name(connector: int, transport_id: str, input_mapping: str | None = None) -> str:
    name = f"output:{mapping_name(connector, transport_id)}"
    return f"{name}+input:{input_mapping}" if input_mapping else name


def target_card_profile(card: Mapping[str, Any], connector: int, transport_id: str,
                        input_mapping: str | None = None) -> str:
    """Card profile that plays ``transport_id`` and keeps the active input.

    The input part of the active profile (for example the analog input of a
    combined HDMI + analog profile) is kept; ``input_mapping`` stands in when
    the card is off. Without that combination the output-only profile is used.
    """
    profiles = card.get("profiles") or {}
    _, active_input = split_card_profile(card.get("active_profile"))
    wanted_input = active_input if card.get("active_profile") not in (None, "", "off") else input_mapping
    for candidate in (card_profile_name(connector, transport_id, wanted_input),
                      card_profile_name(connector, transport_id)):
        if candidate in profiles:
            return candidate
    raise ValueError(f"The card has no {transport(transport_id)['label']} profile for this HDMI connector")


def parse_eld(raw: bytes | None) -> dict | None:
    """Baseline ELD (HDA ELD version 2): receiver name and LPCM descriptors."""
    if not raw or len(raw) < _ELD_FIXED_BYTES or raw[0] >> 3 != 2:
        return None
    name_length = raw[4] & 0x1F
    sad_count = raw[5] >> 4
    start = _ELD_FIXED_BYTES + name_length
    if name_length > _ELD_MAX_MNL or len(raw) < start + 3 * sad_count:
        return None
    lpcm = []
    for index in range(sad_count):
        first, rates, _ = raw[start + 3 * index:start + 3 * index + 3]
        if (first >> 3) & 0x0F != _SAD_FORMAT_LPCM:
            continue
        lpcm.append({"channels": (first & 0x07) + 1,
                     "rates": [rate for bit, rate in enumerate(_SAD_RATES) if rates & (1 << bit)]})
    return {"monitor_name": raw[_ELD_FIXED_BYTES:start].decode("ascii", "replace").strip("\x00 ") or None,
            "connection": "DisplayPort" if (raw[5] >> 2) & 0x03 == 1 else "HDMI",
            "speaker_allocation": raw[7] & 0x7F, "lpcm": lpcm}


def lpcm_max_channels(eld: Mapping | None) -> int | None:
    return max((int(item["channels"]) for item in (eld or {}).get("lpcm") or ()), default=None)


def lpcm_rates(eld: Mapping | None, channels: int) -> list[int]:
    """Rates the receiver accepts for ``channels`` LPCM channels.

    A descriptor covers every channel count up to its own, so the rates of
    all descriptors with at least ``channels`` channels apply.
    """
    rates = {int(rate) for item in (eld or {}).get("lpcm") or ()
             if int(item["channels"]) >= channels for rate in item["rates"]}
    return sorted(rates)


def connector_port(card: Mapping[str, Any], connector: int) -> tuple[str | None, Mapping]:
    """The card port that carries ``connector`` (it lists the connector's profiles)."""
    stereo = card_profile_name(connector, "stereo")
    for name, port in (card.get("ports") or {}).items():
        if stereo in (port.get("profiles") or ()):
            return name, port
    return None, {}


def _detected_channels(port: Mapping) -> int | None:
    try:
        value = int(str((port.get("properties") or {}).get("audio.channels.detected") or ""))
    except ValueError:
        return None
    return value if value > 0 else None


def _profile_available(card: Mapping[str, Any], connector: int, transport_id: str) -> bool | None:
    """Whether the card offers the transport; None when it has no such profile."""
    states = [bool(profile.get("available", True)) for name, profile in (card.get("profiles") or {}).items()
              if split_card_profile(name)[0] == mapping_name(connector, transport_id)]
    if not states:
        return None
    return any(states)


def transport_options(card: Mapping[str, Any], connector: int, eld: Mapping | None) -> list[dict]:
    """The transports of one connector and whether the connected receiver takes them.

    The ELD's LPCM descriptors decide channels and rates; without a readable
    ELD the channel count PipeWire detected on the port stands in, and rates
    stay unrestricted.
    """
    _, port = connector_port(card, connector)
    connected = port.get("availability") != "not available"
    capacity = lpcm_max_channels(eld) or _detected_channels(port)
    receiver = (eld or {}).get("monitor_name") or "the receiver"
    options = []
    for item in TRANSPORTS:
        offered = _profile_available(card, connector, item["id"])
        reason = None
        if offered is None:
            reason = "The card has no profile for this layout"
        elif not connected:
            reason = "No receiver is connected"
        elif capacity is not None and item["channels"] > capacity:
            reason = f"{receiver} accepts at most {capacity} LPCM channels"
        elif not offered:
            reason = "The card reports this layout as unavailable"
        options.append({"id": item["id"], "label": item["label"], "channels": item["channels"],
                        "available": reason is None, "reason": reason,
                        "rates": lpcm_rates(eld, item["channels"]) if eld else []})
    return options


def best_supported(options: Iterable[Mapping], wanted: str | None) -> str | None:
    """Largest available transport not above ``wanted`` (the safe fallback)."""
    if wanted not in _BY_ID:
        return None
    ceiling = _BY_ID[wanted]["channels"]
    fitting = [option for option in options if option.get("available") and option["channels"] <= ceiling]
    return max(fitting, key=lambda option: option["channels"])["id"] if fitting else None


def required_transport(mode: str, assignments: Iterable[str]) -> str:
    """Smallest transport carrying the mode and every assigned physical output."""
    minimum = 2 if mode == "stereo" else 6
    channels = max([minimum] + [index + 1 for index, role in enumerate(assignments) if role != "off"])
    return next((item["id"] for item in TRANSPORTS if item["channels"] >= channels), "7.1")


def routing_key(key: str) -> str:
    """Connector-wide routing identity, independent of its current transport."""
    sink = parse_sink_key(key)
    return sink.identity if sink else key


def stored_routing(routings: Mapping, key: str) -> list | None:
    """Prefer connector routing, then the selected legacy layout, then its widest sibling."""
    sink = parse_sink_key(key)
    if sink is None:
        return routings.get(key)
    if sink.identity in routings:
        return routings[sink.identity]
    if key in routings and sink.transport != "stereo":
        return routings[key]
    for item in reversed(TRANSPORTS):
        legacy = sink_key(sink.card, sink.connector, item["id"])
        if legacy in routings:
            return routings[legacy]
    return None


def routing_ports(channels: int, hardware_ports: Iterable[str] = ()) -> list[str]:
    """Use real port order when complete, otherwise the ACP capacity fallback."""
    ports = list(hardware_ports)
    if len(ports) >= channels:
        return ports[:channels]
    positions = ("FL", "FR", "RL", "RR", "FC", "LFE", "SL", "SR")
    return [f"playback_{position}" for position in positions[:channels]]


def overview_routing_ports(overview: Mapping, channels: int) -> list[str]:
    mode = overview.get("output_mode") or {}
    ports = mode.get("hardware_playback_ports")
    if not ports:
        # Match live discovery, not the suspended sink's raw channel-map order.
        ports = order_hardware_playback_ports(mode.get("hardware_playback_ports_from_channel_map") or ())
    return routing_ports(channels, ports)


def routing_capacity(overview: Mapping) -> int | None:
    """Configurable LPCM capacity, not the active transport's channel count."""
    output = overview.get("selected_output") or overview.get("current_output") or {}
    if not is_hdmi_key(output.get("key")):
        return None
    options = (output.get("hdmi") or {}).get("transports") or []
    available = [int(item["channels"]) for item in options if item.get("available")]
    return max(available, default=int(output.get("channels") or 2))


def _port_suffix(port: object) -> str:
    return str(port or "").rsplit(":", 1)[-1].removeprefix("playback_")


def channel_labels(ports: Iterable[object]) -> list[str | None]:
    """Receiver channel name per hardware port, e.g. ``Center`` or ``LFE``.

    ACP's single RL/RR pair in 5.1 carries the CEA Side surrounds.
    """
    return [_POSITION_LABELS.get(suffix) for suffix in _receiver_positions(ports)]


def _receiver_positions(ports: Iterable[object]) -> list[str]:
    suffixes = [_port_suffix(port) for port in ports]
    if len(suffixes) == 6 and not {"SL", "SR"} & set(suffixes):
        return [{"RL": "SL", "RR": "SR"}.get(suffix, suffix) for suffix in suffixes]
    return suffixes


def default_assignments(ports: Iterable[object], mode: str) -> list[str] | None:
    """Default role per hardware port for one output mode, or None.

    Front L/R play Main, LFE the single sub, and in Surround every other
    receiver channel its own speaker. Ports without a known position stay Off.
    """
    suffixes = _receiver_positions(ports)
    if mode not in _MODE_ROLES or "FL" not in suffixes or "FR" not in suffixes:
        return None
    allowed = _MODE_ROLES[mode]
    return [_DEFAULT_ROLES[suffix] if _DEFAULT_ROLES.get(suffix) in allowed else "off" for suffix in suffixes]


def describe(key: str, *, card: Mapping[str, Any] | None, eld: Mapping | None,
             desired: Mapping | None = None) -> dict | None:
    """Overview payload of one HDMI sink: connector, receiver and transports."""
    sink = parse_sink_key(key)
    if sink is None:
        return None
    card = card or {}
    options = transport_options(card, sink.connector, eld)
    port_name, port = connector_port(card, sink.connector)
    receiver = None
    if eld:
        receiver = {"name": eld.get("monitor_name"), "connection": eld.get("connection"),
                    "lpcm_channels": lpcm_max_channels(eld),
                    "lpcm_rates": lpcm_rates(eld, 2), "source": "eld"}
    elif _detected_channels(port):
        receiver = {"name": None, "connection": None, "lpcm_channels": _detected_channels(port),
                    "lpcm_rates": [], "source": "pipewire"}
    return {"identity": sink.identity, "card": sink.card, "connector": sink.connector,
            "port": port_name, "transport": sink.transport, "transports": options,
            "receiver": receiver, "active_profile": card.get("active_profile"),
            "desired_transport": (desired or {}).get("transport")}


def limit_rates(rates: Iterable[int], hdmi_info: Mapping | None) -> list[int]:
    """Restrict a sink's rates to what the receiver takes on its transport."""
    values = [int(rate) for rate in rates]
    option = next((item for item in (hdmi_info or {}).get("transports") or ()
                   if item["id"] == (hdmi_info or {}).get("transport")), None)
    allowed = set((option or {}).get("rates") or ())
    if not allowed:
        return values
    return [rate for rate in values if rate in allowed] or values


def find_card(cards: Iterable[Mapping], name: str) -> dict | None:
    return next((dict(card) for card in cards or () if card.get("name") == name), None)


def receiver_signature(eld: Mapping | None) -> tuple | None:
    """What identifies a receiver and its LPCM capabilities between probes."""
    if not eld:
        return None
    return (eld.get("monitor_name"),
            tuple((item["channels"], tuple(item["rates"])) for item in eld.get("lpcm") or ()))


class EldCache:
    """Bounded ELD reuse on stable inventory; hotplug invalidates immediately.

    Inventory cannot identify every receiver replacement, so refresh even a
    stable port after 30 seconds. Missing/invalid ELD is never cached.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._entries: dict[str, tuple[object, float, bytes]] = {}

    def discard(self, identity: str) -> None:
        self._entries.pop(identity, None)

    def read(self, identity: str, signature: object, reader) -> bytes | None:
        now = self._clock()
        entry = self._entries.get(identity)
        if entry and entry[0] == signature and now < entry[1]:
            return entry[2]
        self.discard(identity)
        raw = reader()
        if parse_eld(raw) is not None:
            self._entries[identity] = (signature, now + 30.0, raw)
        return raw


def probe_connector(sink: HdmiSink, *, cards: Iterable[Mapping], sinks: Iterable[Mapping],
                    read_eld, pcm_device: int | None = None, eld_cache: EldCache | None = None) -> dict:
    """Live state of one connector, whether or not it currently has a sink.

    ``cards``/``sinks`` are ``pactl -f json`` listings and ``read_eld(card,
    device)`` returns raw ELD bytes. The PCM device comes from the live sink,
    else from ``pcm_device`` (remembered while the sink existed).
    """
    card = find_card(cards, sink.card)
    current = next((row for row in sinks or ()
                    if (parsed := parse_sink_key(row.get("name"))) is not None
                    and parsed.card == sink.card and parsed.connector == sink.connector), None)
    if card is None:
        if eld_cache is not None:
            eld_cache.discard(sink.identity)
        return {"card": None, "current_key": None, "connected": False, "eld": None,
                "options": [], "pcm_device": pcm_device}
    device = str(((current or {}).get("properties") or {}).get("alsa.device") or "")
    device_number = int(device) if device.isdigit() else pcm_device
    card_number = str((card.get("properties") or {}).get("alsa.card") or "")
    _, port = connector_port(card, sink.connector)
    eld = None
    if port.get("availability") == "not available":
        if eld_cache is not None:
            eld_cache.discard(sink.identity)
    elif card_number.isdigit() and device_number is not None:
        def read():
            return read_eld(int(card_number), device_number)
        signature = (card_number, device_number, card.get("active_profile"), json.dumps(port, sort_keys=True))
        eld = parse_eld(eld_cache.read(sink.identity, signature, read) if eld_cache is not None else read())
    # A PCM device that is known but reports no ELD has no receiver behind it.
    connected = port.get("availability") == "available" and (eld is not None or device_number is None)
    return {"card": card, "current_key": (current or {}).get("name"), "connected": connected,
            "eld": eld, "options": transport_options(card, sink.connector, eld),
            "pcm_device": device_number}


def _store_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_root / "fxroute" / "hdmi-transport.json"


def load_desired() -> dict[str, dict]:
    """Transport chosen in FXRoute per connector identity."""
    try:
        payload = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    connectors = payload.get("connectors") if isinstance(payload, dict) else None
    if not isinstance(connectors, dict):
        return {}
    return {identity: dict(record) for identity, record in connectors.items()
            if isinstance(record, dict) and record.get("transport") in _BY_ID}


def store_desired(identity: str, record: dict | None) -> None:
    """Replace (or with None remove) the chosen transport of one connector."""
    connectors = load_desired()
    if record is None:
        connectors.pop(identity, None)
    else:
        transport(record["transport"])
        connectors[identity] = dict(record)
    atomic_write_text(_store_path(), json.dumps({"version": 1, "connectors": connectors},
                                                indent=2, sort_keys=True) + "\n")


def remember_desired(sink: HdmiSink, transport_id: str, *, input_mapping: str | None,
                     pcm_device: int | None) -> None:
    store_desired(sink.identity, {"transport": transport_id, "input": input_mapping,
                                  "pcm_device": pcm_device})


class TransportWatch:
    """Decide automatic transport changes of the selected HDMI connector.

    The application uses automatic routing authority: mode and connector
    routing determine the desired transport, capped at receiver capacity.
    This also corrects outside profile changes on a stable connection. The
    optional non-automatic policy only restores after receiver loss/change;
    there _armed distinguishes reconnects from outside edits. Both policies
    allow WirePlumber one connected tick before acting and retry a target at
    most every RESTORE_RETRY_SECONDS. Automatic downshifts from a supported
    transport require the same narrower target on two consecutive ticks.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._armed: set[str] = set()
        self._receiver: dict[str, object] = {}
        self._connected_ticks: dict[str, int] = {}
        self._attempts: dict[tuple[str, str], float] = {}
        self._downshift: dict[str, str] = {}

    def decide(self, identity: str, *, connected: bool, current: str | None, fallback: str,
               options: list[Mapping], receiver: object, desired: str | None,
               automatic: bool = False) -> str | None:
        if not connected:
            self._downshift.pop(identity, None)
            self._armed.add(identity)
            self._connected_ticks[identity] = 0
            return None
        self._connected_ticks[identity] = self._connected_ticks.get(identity, 0) + 1
        previous = self._receiver.get(identity)
        self._receiver[identity] = receiver
        if previous is not None and previous != receiver:
            self._armed.add(identity)
        armed = identity in self._armed
        wanted = desired if (automatic or armed) and desired else current or desired or fallback
        target = best_supported(options, wanted)
        if target is None or target == current:
            self._downshift.pop(identity, None)
            if current is not None:
                self._armed.discard(identity)
            return None
        downshift = (automatic and current in _BY_ID and
                     best_supported(options, current) == current and
                     _BY_ID[target]["channels"] < _BY_ID[current]["channels"])
        previous_target = self._downshift.pop(identity, None)
        if downshift:
            self._downshift[identity] = target
            if previous_target != target:
                return None
        # WirePlumber restores its saved profile right after a reconnect; give
        # it one tick before acting on the same card.
        if self._connected_ticks[identity] < 2:
            return None
        now = self._clock()
        if now - self._attempts.get((identity, target), float("-inf")) < RESTORE_RETRY_SECONDS:
            return None
        self._attempts[(identity, target)] = now
        return target

    def settled(self, identity: str) -> None:
        self._armed.discard(identity)
