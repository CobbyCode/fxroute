# SPDX-License-Identifier: AGPL-3.0-only
"""Native FXRoute DSP process and PipeWire graph ownership."""

from __future__ import annotations

import asyncio
import math
import os
import re
import signal
import tempfile
import time
import json
import logging
import socket
import shlex
import shutil
import copy
import hashlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Mapping, Sequence

from audio.output_topology import SURROUND_POSITIONS
from audio.output_ports import HARDWARE_CHANNEL_ORDER, hardware_link_target
from audio.pw_link import stop_command_child_cancellation_safe
from common.run_to_completion import restore_after, run_to_completion
from dsp.latency import latency_signature

# Sources that must reach the hardware only through the DSP output stage.
DIRECT_SOURCE_NODES = ("mpv", "spotify")
DSP_NODE_NAME = "fxroute_dsp"
DSP_INGRESS_MONITOR_NODE = "fxroute_dsp_sink"
DSP_INGRESS_PORTS = ("monitor_FL", "monitor_FR")
DSP_POST_EFFECT_PORTS = ("post_effect_FL", "post_effect_FR")
DEFAULT_SAMPLE_RATE = 48_000
RUNTIME_COMMAND_TIMEOUT_SECONDS = 5.0
RUNTIME_COMMAND_TERMINATE_GRACE_SECONDS = 2.0
RUNTIME_COMMAND_TIMEOUT_RETURNCODE = -1
RUNTIME_ORPHAN_KILL_GRACE_SECONDS = 0.5
HELPER_STDERR_TAIL_LIMIT = 64 * 1024
# Largest accepted native DSP control reply.  The control channel is a
# datagram socket: an oversized reply is silently truncated at the receive
# buffer, so the reader requests one extra byte and treats a full buffer as
# truncation instead of parsing a partial payload.
CONTROL_REPLY_MAX_BYTES = 16384
CONTROL_TIMEOUT_SECONDS = 1.0
# Float32 gain storage and %.9g replies round-trip within 1e-5 dB over [-80, 0].
OUTPUT_GAIN_TOLERANCE_DB = 1e-5
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class PipeWireLink:
    source: str
    target: str


def decoded_ingress_edges(input_count: int) -> tuple[tuple[str, str], ...]:
    """Discrete Surround positions or the complete stereo downmix, never Front-only."""
    positions = SURROUND_POSITIONS if input_count == 8 else ("stereo_FL", "stereo_FR")
    return tuple((f"fxroute_surround_input:output_{position}", f"{DSP_NODE_NAME}:input_{index + 1}")
                 for index, position in enumerate(positions))


def mapped_ingress_edges(
    source: str,
    suffixes: Mapping[str, str],
    sources: Mapping[str, str] | None = None,
    port_kind: str = "capture",
) -> tuple[tuple[str, str], ...]:
    """Direct source-port -> engine-input edges of a multichannel ingress.

    ``suffixes`` maps program positions to channel suffixes; the engine
    input of a position is its place in ``SURROUND_POSITIONS``. ``sources``
    names the node of a position, ``source`` is the node of every position
    it does not list. ``port_kind`` is ``capture`` for a capture node and
    ``output`` for a playback stream such as the STDIN adapter.
    """
    nodes = sources or {}
    return tuple(
        (f"{nodes.get(position) or source}:{port_kind}_{suffixes[position]}", f"{DSP_NODE_NAME}:input_{index + 1}")
        for index, position in enumerate(SURROUND_POSITIONS)
        if position in suffixes
    )


def _contains_link(text: str, source: str, target: str) -> bool:
    """Return whether ``source`` is linked to ``target`` in ``pw-link -l`` text.

    The classic three fixed patterns fail when another link to the same
    target port is listed before the searched one (e.g. Spotify connected to
    fxroute_dsp_sink before mpv): the ``|<- source`` line is then not
    adjacent to the target header.  Parse the port blocks line-wise instead:
    each non-link line is the current port header, each ``|<-``/``|->`` line
    is a link of that port.  Link order within a port block is irrelevant.
    """
    if not text or source not in text or target not in text:
        return False
    if f"{source} -> {target}" in text:
        return True
    current_port: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("|"):
            if current_port is None:
                continue
            if f"|<- {source}" in raw and current_port == target:
                return True
            if f"|-> {target}" in raw and current_port == source:
                return True
        else:
            current_port = line
    return False


def iter_pw_links(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(source, target)`` for every edge in ``pw-link -l`` text.

    Each non-link line is the current port header, each ``|->``/``|<-`` line a
    link of that port; the ``source -> target`` form is accepted as well.  One
    parser for the command's output keeps every consumer's view of the live
    graph identical.
    """
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("|-> "):
            yield current, line[4:].strip()
        elif line.startswith("|<- "):
            yield line[4:].strip(), current
        elif " -> " in line:
            source, target = line.split(" -> ", 1)
            yield source.strip(), target.strip()
        else:
            current = line


# A `pw-link -l` port header: "<node>:<port>" with no spaces.  Real output
# names every port this way (ALSA keys carry dots/dashes but no spaces);
# error text and unknown future formats do not.
_PW_LINK_PORT_RE = re.compile(r"^\S+:\S+$")


def pw_link_output_unrecognized(text: str, edges: Sequence[tuple[str, str]]) -> bool:
    """Return whether `pw-link -l` text has no recognizable graph shape.

    Empty output is the normal "no links" shape.  Otherwise at least one
    port header or one well-formed edge (both sides "<node>:<port>") must be
    present; anything else is error text or an unknown format, where an
    absent candidate proves nothing and the caller must not treat the graph
    as clean.
    """
    if not text.strip():
        return False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("|-> ") or stripped.startswith("|<- "):
            continue
        if " -> " in stripped:
            continue
        if _PW_LINK_PORT_RE.match(stripped):
            return False
    return not any(
        source and target and ":" in source and ":" in target
        for source, target in edges
    )


def physical_output_links(text: str) -> set[PipeWireLink]:
    """Read DSP-to-playback edges, preserving capture and post-effect taps."""
    return {
        PipeWireLink(source, target)
        for source, target in iter_pw_links(text)
        if re.fullmatch(r"fxroute_dsp:output_\d+", source) and ":playback_" in target
    }


def _finite_number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default



@dataclass(frozen=True)
class BassManagementConfig:
    """Canonical normalized output/bass-management configuration.

    ``from_overview`` is the single place that interprets, normalizes and
    clamps output mode, output selection, sample rate, crossover, Main
    high-pass, Sub1/Sub2 level/alignment/polarity, derived delays and
    Mono/Stereo bass routing.  The DSP runtime, measurement and AutoSub all
    consume this one representation.
    """

    output_mode: str
    output_key: str
    output_label: str
    output_channels: int
    sample_rate: int
    crossover_frequency_hz: int
    main_highpass_enabled: bool
    sub_level_db: float
    sub_alignment_ms: float
    sub_polarity: str
    sub2_level_db: float = 0.0
    sub2_alignment_ms: float = 0.0
    sub2_polarity: str = "normal"

    @property
    def derived_main_delay_ms(self) -> float:
        alignments = (self.sub_alignment_ms, self.sub2_alignment_ms) if self.output_mode.startswith("subwoofer-2.2") else (self.sub_alignment_ms,)
        return max(0.0, -min(alignments))

    @property
    def derived_sub_delay_ms(self) -> float:
        return self.derived_sub1_delay_ms

    @property
    def derived_sub1_delay_ms(self) -> float:
        return self.derived_main_delay_ms + self.sub_alignment_ms if self.output_mode.startswith("subwoofer-2.2") else max(0.0, self.sub_alignment_ms)

    @property
    def derived_sub2_delay_ms(self) -> float:
        return self.derived_main_delay_ms + self.sub2_alignment_ms if self.output_mode.startswith("subwoofer-2.2") else self.derived_sub_delay_ms

    @property
    def bass_routing(self) -> str:
        return "stereo" if self.output_mode == "subwoofer-2.2-stereo" else "mono"

    @classmethod
    def from_overview(cls, overview: dict[str, Any]) -> "BassManagementConfig":
        mode = overview.get("output_mode") or {}
        output = overview.get("selected_output") or overview.get("current_output") or {}
        name = str(mode.get("mode") or "stereo")
        output_key = str(mode.get("effective_output_key") or output.get("key") or output.get("name") or "").strip()
        rate = int(mode.get("effective_output_rate") or output.get("active_rate") or overview.get("active_rate") or DEFAULT_SAMPLE_RATE)
        channels = int(mode.get("effective_output_channels") or output.get("channels") or 0)
        if name.startswith("subwoofer-2.2"):
            subs = mode.get("subwoofers") or {}
            sub1, sub2 = subs.get("sub1") or {}, subs.get("sub2") or {}
            frequency = max(40, min(200, int(round(_finite_number(mode.get("crossover_frequency_hz"), 80)))))
            highpass = bool(mode.get("main_highpass_enabled", True))
            values = (sub1, sub2)
            levels = tuple(max(-80.0, min(12.0, _finite_number(item.get("level_db")))) for item in values)
            alignments = tuple(max(-40.0, min(40.0, round(_finite_number(item.get("alignment_ms")), 2))) for item in values)
            polarities = tuple("invert" if str(item.get("polarity") or "").lower() in {"invert", "inverted", "180"} else "normal" for item in values)
        else:
            sub = mode.get("subwoofer") or {}
            frequency = max(40, min(200, int(round(_finite_number(sub.get("crossover_frequency_hz"), 80)))))
            highpass = bool(sub.get("main_highpass_enabled", True))
            level = max(-24.0, min(12.0, _finite_number(sub.get("sub_level_db"))))
            alignment = max(-40.0, min(40.0, round(_finite_number(sub.get("sub_alignment_ms")), 2)))
            polarity = "invert" if str(sub.get("sub_polarity") or "").lower() in {"invert", "inverted", "180"} else "normal"
            levels, alignments, polarities = (level, level), (alignment, alignment), (polarity, polarity)
        return cls(name, output_key, str(output.get("label") or output.get("target_label") or output_key or "unknown output"),
                   channels, rate, frequency, highpass, levels[0], alignments[0], polarities[0], levels[1], alignments[1], polarities[1])


@dataclass(frozen=True)
class DSPRuntimeConfig:
    output_mode: str
    output_key: str
    sample_rate: int
    hardware_ports: tuple[str, ...]
    layout: tuple[dict[str, Any], ...]
    output_routes: tuple[tuple[int, str], ...] | None = None
    plan_fingerprint: str | None = None

    @property
    def input_count(self) -> int:
        return 8 if self.output_mode == "surround" else 2

    @property
    def route_pairs(self) -> tuple[tuple[int, str], ...]:
        if self.output_routes is not None:
            return self.output_routes
        return tuple(enumerate(self.hardware_ports, 1))

    @classmethod
    def from_plan(cls, plan: Mapping[str, Any], *, layout: Sequence[dict[str, Any]],
                  output_key: str, sample_rate_hz: int,
                  hardware_ports: Sequence[str] | None,
                  plan_fingerprint: str | None = None) -> "DSPRuntimeConfig":
        """Build a runtime config from a compiled processing plan.

        The plan carries explicit role-indexed physical edges, and only
        those hardware channels are linked. Malformed plans fail instead of
        falling back to semantic port names.
        """
        if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
            raise RuntimeError("Planned sample rate must be a positive integer")
        ports = [str(port) for port in (hardware_ports or []) if str(port)]
        if not ports:
            raise RuntimeError("No hardware playback ports for planned output")
        edges = plan.get("physical_routes") if isinstance(plan, Mapping) else None
        if not isinstance(edges, list) or not edges:
            raise RuntimeError("Processing plan has no physical routes")
        rows = tuple(dict(entry) for entry in layout)
        routes = []
        for edge in edges:
            if not isinstance(edge, Mapping):
                raise RuntimeError("Processing plan has a malformed physical route")
            output = edge.get("output")
            channel = edge.get("channel")
            if type(output) is not int or type(channel) is not int:
                raise RuntimeError("Processing plan has a malformed physical route")
            if output < 0 or channel < 0:
                raise RuntimeError("Processing plan has a negative physical route")
            if output >= len(rows):
                raise RuntimeError("Processing plan routes an unknown logical output")
            if channel >= len(ports):
                raise RuntimeError(
                    f"Processing plan needs hardware channel {channel}, "
                    f"but {output_key or 'the selected output'} exposes {len(ports)}")
            routes.append((output + 1, ports[channel]))
        mode = plan.get("mode") if isinstance(plan, Mapping) else None
        return cls(str(mode or ""), str(output_key or ""), sample_rate_hz,
                   tuple(ports), rows, tuple(routes), plan_fingerprint)


def config_route_pairs(config: Any) -> tuple[tuple[int, str], ...]:
    """Physical (signal, port) edges for a runtime config object.

    Duck-typed configs in older tests carry only ``hardware_ports``; those
    keep the historic positional mapping instead of failing on the missing
    ``route_pairs`` attribute.
    """
    routes = getattr(config, "route_pairs", None)
    if routes is not None:
        return routes
    return tuple(enumerate(getattr(config, "hardware_ports", ()), 1))


@dataclass(frozen=True)
class PlannedSyncTarget:
    """Prebuilt v2 sync target: plan-derived config plus rendered engine text."""
    config: DSPRuntimeConfig
    text: str
    on_applied: Callable[[], None] | None = None
    validate_current: Callable[[], None] | None = None

class DSPRuntime:
    def __init__(self, manager: Any, *, binary: str | Path | None = None,
                 command_runner: Callable[[Sequence[str]], Awaitable[CommandResult]] | None = None,
                 process_launcher: Callable[[Sequence[str]], Awaitable[Any]] | None = None):
        self.manager = manager
        self.binary = Path(binary or os.environ.get("FXROUTE_DSP_BINARY") or Path(__file__).parent.parent / "native_dsp/build/fxroute-dsp")
        self._run = command_runner or self._run_command
        self._launch = process_launcher or self._launch_process
        self._realtime_launcher = os.environ.get("FXROUTE_DSP_RT_LAUNCHER", "")
        self._process = None
        self._config: DSPRuntimeConfig | None = None
        self._config_path: Path | None = None
        self._config_text: str | None = None
        self._links: list[PipeWireLink] = []
        self._lock = asyncio.Lock()
        self._measurement_scope_lock = asyncio.Lock()
        self._measurement_active = False
        self._control_lock = asyncio.Lock()
        # Serializes the whole ownership decision (state read, validation,
        # engine command with its acknowledgement, state write) across the
        # exact sub mute and output mask APIs. The plain control lock only
        # serializes commands and replies, so overlapping calls could each
        # decide ownership before either acknowledgement lands.
        self._mute_ownership_lock = asyncio.Lock()
        self._error: str | None = None
        self._started_at: float | None = None
        self._control_socket: socket.socket | None = None
        self._control_path: Path | None = None
        self._control_client_path: Path | None = None
        self._control_poisoned = False
        self._exact_sub_mute = False
        self._exact_sub_mute_mask = 0
        self._effect_bypass = False
        self._output_gain_db = 0.0
        self._output_mask = 0
        self._input_mask = 0
        self._stderr_drain_task: asyncio.Task | None = None
        self._stderr_tail = b""
        self._surround_input = False
        self._measurement_surround_input = False
        self._measurement_ingress_restore: dict = {}
        self._mapped_ingress: tuple[tuple[str, str], ...] = ()
        self._measurement_mapped_ingress: tuple[tuple[str, str], ...] = ()
        self._latency_snapshot: dict = {}
        self._latency_tracking = False
        self._input_latency_provider: Callable[[], float | Awaitable[float]] | None = None
        self._last_input_latency_ms: float | None = None
        self._measurement_latency_signature: tuple | None = None
        self._measurement_latency_snapshot: dict = {}
        self._measurement_latency_owner: asyncio.Task | None = None
        self._measurement_peak_capture_id: str | None = None

    def configure_latency_tracking(self, *, input_latency_provider: Callable[[], float | Awaitable[float]]) -> None:
        self._latency_tracking = True
        self._input_latency_provider = input_latency_provider

    async def _input_latency_ms(self) -> float:
        value = self._input_latency_provider() if self._input_latency_provider else 0.0
        if inspect.isawaitable(value):
            value = await value
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 20000:
            raise RuntimeError('Input latency is unavailable')
        return value

    def _latency_ingress_owner(self) -> tuple:
        return self._surround_input, self._mapped_ingress, self._measurement_active

    def _latency_owner(self) -> tuple:
        return self._process, self._config, self._latency_ingress_owner(), self.sync_in_progress

    def _latency_owner_matches(self, owner: tuple) -> bool:
        process, config, ingress_owner, was_syncing = owner
        return (self._process is process and self._config is config
                and self._latency_ingress_owner() == ingress_owner
                and (not self.sync_in_progress or was_syncing))

    async def update_input_latency(self) -> None:
        if not self._latency_tracking or not self._engine_running():
            return
        owner = self._latency_owner()
        value = await self._input_latency_ms()
        async with self._control_lock:
            if not self._latency_owner_matches(owner) or not self._engine_running():
                return
            if value != self._last_input_latency_ms:
                self._last_input_latency_ms = None
                await self._control_unlocked(f'input latency {value:.9g}', reply=True)
                if self._latency_owner_matches(owner) and self._engine_running():
                    self._last_input_latency_ms = value

    async def read_latency(self) -> dict:
        if not self._engine_running() or self._config is None or self.sync_in_progress:
            return {'ready': False}
        owner = self._latency_owner()
        config = self._config
        async with self._control_lock:
            if not self._latency_owner_matches(owner) or not self._engine_running():
                raise RuntimeError('Native latency rendering changed during control wait')
            payload = json.loads(await self._control_unlocked('latency get', reply=True))
            if not self._latency_owner_matches(owner) or not self._engine_running():
                raise RuntimeError('Native latency rendering changed during readback')
        if not isinstance(payload, dict) or not payload.get('ready'):
            self._latency_snapshot = {'ready': False}
            return copy.deepcopy(self._latency_snapshot)
        rows = payload.get('outputs')
        if (self._config is not config or payload.get('rate') != config.sample_rate
                or not isinstance(rows, list) or len(rows) != len(config.layout)):
            raise RuntimeError('Native latency does not match the active rendering')
        rate = payload['rate']
        for key in ('input', 'target', 'minimum', 'maximum', 'common', 'actual'):
            value = payload.get(key)
            if type(value) is not int or value < 0:
                raise RuntimeError('Native latency contains invalid frames')
            payload[f'{key}_ms'] = value * 1000.0 / rate
        for index, row in enumerate(rows):
            row['role'] = str(config.layout[index].get('name', ''))
            row.setdefault('adaptive', 0)
            for key in ('dsp', 'intentional', 'align', 'balance', 'sync', 'total', 'adaptive'):
                value = row.get(key)
                if type(value) is not int or value < 0:
                    raise RuntimeError('Native output latency contains invalid frames')
                row[f'{key}_ms'] = value * 1000.0 / rate
        positions = SURROUND_POSITIONS if config.input_count == 8 else ('FL', 'FR')
        for index, row in enumerate(payload.get('sources', [])):
            if index >= len(positions):
                raise RuntimeError('Native latency contains an unknown source')
            row['position'] = positions[index]
            row['roles'] = [str(channel.get('name', '')) for channel in config.layout
                            if any(send.get('input') == index and send.get('gain', 0) != 0
                                   for send in (channel.get('routes', [])
                                                + channel.get('post_crossover_routes', [])
                                                + [send for group in channel.get('crossover_groups', [])
                                                   for send in group['routes']]))]
            row['active'] = bool(row['roles'])
            row.setdefault('adaptive', 0)
            for key in ('dsp', 'balance', 'adaptive'):
                if type(row.get(key)) is not int or row[key] < 0:
                    raise RuntimeError('Native source latency contains invalid frames')
                row[f'{key}_ms'] = row[key] * 1000.0 / rate
        payload['context'] = {'output_key': config.output_key,
                              'plan_fingerprint': config.plan_fingerprint,
                              'rendering': hashlib.sha256((self._config_text or '').encode()).hexdigest(),
                              'timing_key': self._timing_key()}
        self._latency_snapshot = payload
        return copy.deepcopy(payload)

    async def settled_latency(self) -> dict:
        owner = self._latency_owner()
        expected_bypass = self._effect_bypass
        await self.update_input_latency()
        if not self._latency_owner_matches(owner) or self.sync_in_progress:
            raise RuntimeError('Native latency rendering changed during graph read')
        expected_input = (round(self._last_input_latency_ms * self._config.sample_rate / 1000)
                          if self._last_input_latency_ms is not None and self._config else None)
        for _ in range(20):
            value = await self.read_latency()
            if not self._latency_owner_matches(owner) or self.sync_in_progress:
                raise RuntimeError('Native latency rendering changed during readback')
            if (value.get('ready') and bool(value.get('bypassed')) == expected_bypass
                    and (expected_input is None or value.get('input') == expected_input)):
                return value
            await asyncio.sleep(0.025)
        raise RuntimeError('Native latency readback is not ready')

    def _timing_key(self) -> str:
        """Rendering identity excluding measurement-owned zero-latency controls."""
        lines = [line for line in (self._config_text or '').splitlines()
                 if line.split() and line.split()[0] not in
                 {'output', 'sos', 'peq', 'xsos', 'gxsos', 'avsync', 'input_latency', 'bypass'}]
        config = self._config
        identity = (config.output_key, config.sample_rate, config.output_mode,
                    tuple(row.get('name') for row in config.layout), lines) if config else ()
        return hashlib.sha256(repr(identity).encode()).hexdigest()

    def freeze_measurement_latency(self, snapshot: dict) -> None:
        if not snapshot.get('ready'):
            raise RuntimeError('Measurement latency readback is unavailable')
        self._measurement_latency_signature = latency_signature(snapshot)
        self._measurement_latency_snapshot = copy.deepcopy(snapshot)
        if self._measurement_ingress_restore:
            self._measurement_ingress_restore['sweep_input'] = snapshot['input']
        self._measurement_latency_owner = asyncio.current_task()

    def validate_measurement_latency(self, snapshot: dict) -> None:
        if (self._measurement_latency_signature is not None
                and (not snapshot.get('ready') or latency_signature(snapshot) != self._measurement_latency_signature)):
            raise RuntimeError('DSP/input latency changed during measurement; restart calibration')

    async def begin_measurement_peak_capture(self, capture_id: str) -> None:
        """Reset once after preroll; retain every retry's peaks in this scope."""
        async with self._control_lock:
            if (not self._measurement_scope_lock.locked()
                    or self._measurement_latency_signature is None):
                raise RuntimeError('Measurement peak scope is unavailable')
            if not isinstance(capture_id, str) or not capture_id:
                raise ValueError('Measurement peak capture requires a capture id')
            if self._measurement_peak_capture_id == capture_id:
                return
            if self._measurement_peak_capture_id is not None:
                raise RuntimeError('Measurement peak capture belongs to another job')
            await self._control_unlocked('peaks reset', reply=True)
            self._measurement_peak_capture_id = capture_id

    async def finish_measurement_latency(self, job: dict) -> dict:
        """Validate the final capture block before accepting any worker result."""
        if not self._latency_tracking:
            return {}
        self._measurement_latency_snapshot.pop('verified', None)
        if self._measurement_latency_signature is None:
            raise RuntimeError('Measurement latency scope is unavailable')
        snapshot = await self.settled_latency()
        self.validate_measurement_latency(snapshot)
        if self._measurement_peak_capture_id is not None:
            if self._measurement_peak_capture_id != job.get('id'):
                raise RuntimeError('Measurement peak capture belongs to another job')
            snapshot['output_peaks'] = await run_to_completion(self.read_output_peaks())
        snapshot['verified'] = True
        snapshot['capture_id'] = job.get('id')
        if self._measurement_ingress_restore:
            snapshot['ingress_restore'] = copy.deepcopy(self._measurement_ingress_restore)
        target_roles = list((job.get('measurement_target') or {}).get('measured_roles') or [])
        if target_roles:
            snapshot['measured_roles'] = target_roles
        elif job.get('measurement_scope') == 'raw_helper':
            snapshot['measured_roles'], snapshot['measured_positions'] = self._raw_helper_coverage(job)
        else:
            snapshot['measured_roles'] = [row['role'] for row in snapshot['outputs']]
        self._measurement_latency_snapshot = copy.deepcopy(snapshot)
        return snapshot

    def _raw_helper_coverage(self, job: dict) -> tuple[list, list]:
        """Roles and source positions one raw sweep actually excited.

        A raw child bypasses the active chain and carries no measurement
        target, so the whole layout is not evidence. The sweep channel
        selects the excited program positions (left FL, right FR); an
        output is measured only when unmuted and fed by an excited
        position through any route, including post-crossover and grouped
        bass sends. A source position is measured only when excited itself
        and reaching an unmuted output, so an unexcited LFE never inherits
        a sub's measured basis from its shared output role.
        """
        channel = str(job.get('channel') or '').strip().lower()
        config = self._config
        positions = (SURROUND_POSITIONS if config is not None and config.input_count == 8
                     else ('FL', 'FR'))
        if channel == 'left':
            excited = ['FL']
        elif channel == 'right':
            excited = ['FR']
        elif channel == 'stereo':
            excited = ['FL', 'FR']
        else:
            excited = list(positions)
        excited = [position for position in excited if position in positions]
        excited_indices = {positions.index(position) for position in excited}
        input_muted = int(getattr(self, '_input_mask', 0) or 0)
        excited_indices = {index for index in excited_indices if not (input_muted >> index) & 1}
        muted = int(getattr(self, '_output_mask', 0) or 0) | int(
            getattr(self, '_exact_sub_mute_mask', 0) or 0)
        layout = tuple(getattr(config, 'layout', ()) or ()) if config is not None else ()
        roles: list[str] = []
        reaching: set[str] = set()
        for index, entry in enumerate(layout):
            if (muted >> index) & 1:
                continue
            sends = [send for send in (entry.get('routes', []) or [])
                     if send.get('gain', 0) != 0]
            sends += [send for send in (entry.get('post_crossover_routes', []) or [])
                      if send.get('gain', 0) != 0]
            for group in entry.get('crossover_groups', []) or []:
                sends += [send for send in (group.get('routes', []) or [])
                          if send.get('gain', 0) != 0]
            fed = {send.get('input') for send in sends} & excited_indices
            if fed:
                roles.append(str(entry.get('name', '')))
                for position_index in fed:
                    reaching.add(positions[position_index])
        measured_positions = [position for position in excited if position in reaching]
        return roles, measured_positions

    async def latency_watch_loop(self, *, on_ready: Callable[[dict], Awaitable[None]] | None = None) -> None:
        while True:
            await asyncio.sleep(0.1)
            if not self._engine_running() or self._lock.locked() or self._control_poisoned:
                continue
            try:
                await self.update_input_latency()
                value = await self.read_latency()
                try:
                    self.validate_measurement_latency(value)
                except RuntimeError:
                    owner = self._measurement_latency_owner
                    if owner is not None and not owner.done():
                        logger.error('DSP/input latency changed; cancelling measurement')
                        owner.cancel()
                    continue
                if value.get('ready') and on_ready is not None and not self._measurement_scope_lock.locked():
                    await on_ready(value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('AV Sync readback unavailable: %s', exc)

    @property
    def sync_in_progress(self) -> bool:
        return self._lock.locked()

    def _engine_running(self) -> bool:
        return self._process is not None and getattr(self._process, "returncode", None) is None

    def output_meter_layout(self) -> tuple[str, tuple[str, ...]] | None:
        """Running engine's output mode and output role names, in output order."""
        config = self._config
        if config is None or not self._engine_running():
            return None
        return config.output_mode, tuple(str(channel.get("name", "")) for channel in config.layout)

    @property
    def surround_engine(self) -> bool:
        """Whether the running engine exposes the eight Surround program inputs."""
        return bool(self._config and self._engine_running()
                    and self._config.input_count == len(SURROUND_POSITIONS))

    def read_loudness_runtime(self,
                              extras: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return the confirmed Loudness work point for the given extras.

        The work point is a deterministic derivation of the confirmed extras
        (the manager's persisted extras unless ``extras`` is passed).  The
        engine is the authority on liveness: without a running native engine
        there is no confirmed live state to report.
        """
        if not self._engine_running():
            raise RuntimeError("Native DSP engine is not active; Loudness runtime readback is unavailable")
        source = self.manager.load_global_extras() if extras is None else extras
        normalized = self.manager.normalize_effects_extras(dict(source))
        payload = self.manager._loudness_plugin_payload(normalized["loudness"], normalized["autogain"])
        return {"volume": float(payload["volume"]), "output_gain": float(payload["output-gain"]),
                "bypass": bool(payload["bypass"])}

    def read_autogain_runtime(self,
                              extras: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return the confirmed AutoGain work point for the given extras."""
        if not self._engine_running():
            raise RuntimeError("Native DSP engine is not active; AutoGain runtime readback is unavailable")
        source = self.manager.load_global_extras() if extras is None else extras
        normalized = self.manager.normalize_effects_extras(dict(source))
        payload = self.manager._autogain_plugin_payload(normalized["autogain"])
        return {"target": float(payload["target"]), "bypass": bool(payload["bypass"])}

    async def _reclean_guarded(self, skip_if_locked: bool = False) -> bool:
        if skip_if_locked and self._lock.locked():
            return False
        async with self._lock:
            await self._remove_direct_source_links()
            if self._config is not None:
                await self._reconcile_output_links(self._config)
            if await self.verify():
                return True
            if self._config is None:
                return False
            for link in self._links:
                await self._run(("pw-link", link.source, link.target))
            return await self.verify()

    async def _remove_direct_source_links(self) -> None:
        if self._config is None:
            return
        # The source nodes keep their fixed output_FL/FR/RL/RR naming; only
        # the hardware side is resolved, so a direct source→sink link is
        # removed no matter how the device names its playback ports.
        # Split-group ports are already qualified (sink:port).
        candidates = {
            PipeWireLink(f"{node}:output_{channel}", hardware_link_target(self._config.output_key, port))
            for node in DIRECT_SOURCE_NODES
            for port in self._config.hardware_ports
            for channel in HARDWARE_CHANNEL_ORDER
        }
        # Disconnecting the whole candidate matrix costs one failing `pw-link
        # -d` subprocess per combination (144 on an 18-channel device) and
        # every one of them is a no-op on a healthy graph.  Read the live graph
        # once instead and only disconnect edges that actually exist, which is
        # the same set but a single `pw-link -l` on the common path.
        result = await self._run(("pw-link", "-l"))
        if result.returncode:
            raise RuntimeError(result.stderr or "Cannot inspect direct source links")
        parsed = list(iter_pw_links(result.stdout))
        if pw_link_output_unrecognized(result.stdout, parsed):
            # Fail-safe: the output has no recognizable graph shape, so an
            # absent candidate proves nothing.  Remove the whole matrix as
            # before the single-read optimization; never taken on success.
            logger.warning("Direct source link inspection unrecognized; removing full candidate matrix")
            for node in DIRECT_SOURCE_NODES:
                for port in self._config.hardware_ports:
                    for channel in HARDWARE_CHANNEL_ORDER:
                        await self._run((
                            "pw-link", "-d", f"{node}:output_{channel}",
                            hardware_link_target(self._config.output_key, port),
                        ))
            return
        live = {PipeWireLink(source, target) for source, target in parsed}
        for link in live & candidates:
            await self._run(("pw-link", "-d", link.source, link.target))

    async def reclean_direct_dsp_links(self) -> None:
        await self._reclean_guarded()

    def snapshot(self) -> dict[str, Any]:
        running = self._process is not None and getattr(self._process, "returncode", None) is None
        return {"active": running and not self._error and not self._control_poisoned and bool(self._links), "engine": "fxroute_native_dsp",
                "helper_pid": getattr(self._process, "pid", None) if running else None,
                "helper_args": [str(self.binary), str(self._config_path)] if self._config_path else None,
                "config": {"sample_rate": self._config.sample_rate, "output_mode": self._config.output_mode,
                            "output_key": self._config.output_key,
                            "hardware_ports": list(self._config.hardware_ports),
                            "output_routes": list(config_route_pairs(self._config)),
                            "plan_fingerprint": getattr(self._config, "plan_fingerprint", None),
                            "layout": [dict(channel) for channel in getattr(self._config, "layout", ())]} if self._config else None,
                "last_error": self._error, "last_started_at": self._started_at,
                 "links_configured": bool(self._links), "exact_sub_mute": self._exact_sub_mute,
                 "surround_input": self._surround_input,
                 "mapped_ingress": [list(edge) for edge in self._mapped_ingress],
                "output_mask": None if self._control_poisoned else self._output_mask,
                "input_mask": None if self._control_poisoned else self._input_mask,
                "control_state": "unknown" if self._control_poisoned else "ready",
                 "effect_bypass": self._effect_bypass, "output_gain_db": self._output_gain_db,
                 "latency_tracking": self._latency_tracking,
                "av_sync": copy.deepcopy(self._latency_snapshot),
                 "measurement_latency": copy.deepcopy(self._measurement_latency_snapshot),
                  "measurement_active": self._measurement_active,
                "stderr_tail": self.stderr_tail()[:2048]}

    async def set_exact_sub_mute(self, enabled: bool, *, mask: int | None = None) -> bool:
        """Set exact sub mute, returning its previously acknowledged enabled state.

        Omitted masks retain legacy outputs 3/4. Explicit masks address engine
        outputs in plan order. Restore with the returned boolean and the SAME
        mask; changing an active mask is rejected because a boolean cannot
        represent the old mask. Independent output-mask bits remain muted.
        """
        async with self._mute_ownership_lock:
            self._require_control_state()
            if mask is None and len(self._config.hardware_ports if self._config else ()) < 4:
                raise RuntimeError("Sub outputs are unavailable")
            bits = self._validate_output_mask(12 if mask is None else mask)
            layout = tuple(getattr(self._config, "layout", ()) or ())
            output_count = len(layout) if layout else min(4, len(self._config.hardware_ports if self._config else ()))
            if bits >= (1 << output_count):
                raise ValueError("Output mute mask addresses an output the engine does not expose")
            previous = self._exact_sub_mute
            if previous and bits != self._exact_sub_mute_mask:
                raise RuntimeError("Cannot change an active exact sub mute mask; restore it first")
            control_bits = bits if enabled else bits & ~self._output_mask
            if control_bits:
                await self._control(f"mute {control_bits} {1 if enabled else 0}", reply=True)
            self._exact_sub_mute = bool(enabled)
            self._exact_sub_mute_mask = bits if enabled else 0
            return previous

    def _validate_output_mask(self, mask: object) -> int:
        """Validate a role-derived engine output mute mask.

        Bit ``n`` addresses engine output ``n`` in plan order.  A zero mask is
        rejected instead of silently no-opping, so callers must state that no
        masking is needed; the native engine owns one 32-bit mask.
        """
        if type(mask) is not int or mask <= 0 or mask >= (1 << 32):
            raise ValueError("Output mute mask must be a non-zero 32-bit integer")
        layout = tuple(getattr(self._config, "layout", ()) or ()) if self._config is not None else ()
        if layout and mask >= (1 << len(layout)):
            raise ValueError("Output mute mask addresses an output the engine does not expose")
        return mask

    def _require_control_state(self) -> None:
        if self._control_poisoned:
            raise RuntimeError("Native DSP control state is unknown; rebuild the engine")

    @staticmethod
    def _validate_input_mask(mask: object) -> int:
        """Validate an input-position mute mask (0 means none).

        Bit ``n`` zeroes engine input ``n`` before the stage chain; the native
        engine owns one 32-bit mask.
        """
        if type(mask) is not int or mask < 0 or mask >= (1 << 32):
            raise ValueError("Input mute mask must be a 32-bit non-negative integer")
        return mask

    async def apply_output_mask(self, mask: int, *, input_mask: int = 0) -> int:
        """Mute the masked engine outputs; returns the previously applied mask.

        Only the given bits are set: unrelated bits (for example the legacy
        exact-sub mute) stay untouched, which is why clearing must repeat the
        same mask rather than resetting the engine's whole mute state. The
        engine applies the mute before it acknowledges, so the command, its
        acknowledgement and the recorded mask finish as one unit; a cancel is
        re-raised afterwards. ``input_mask`` additionally zeroes engine input
        positions, for a sweep whose sink feed would otherwise reach an audible
        output through the bass bus.
        """
        async with self._mute_ownership_lock:
            self._require_control_state()
            bits = self._validate_output_mask(mask)
            input_bits = self._validate_input_mask(input_mask)
            previous = self._output_mask

            async def exchange() -> None:
                await self._control(f"mute {bits} 1", reply=True)
                self._output_mask |= bits
                if input_bits:
                    await self._control(f"inmute {input_bits} 1", reply=True)
                    self._input_mask |= input_bits

            await run_to_completion(exchange())
            return previous

    async def clear_output_mask(self, mask: int, *, input_mask: int = 0) -> None:
        """Release output-mask bits without clearing an active exact sub mute.

        Like ``apply_output_mask``, the engine command and the recorded mask
        finish together even when the caller is cancelled meanwhile. The input
        mask is released whether or not the output command succeeded.
        """
        async with self._mute_ownership_lock:
            self._require_control_state()
            bits = self._validate_output_mask(mask)
            input_bits = self._validate_input_mask(input_mask)
            control_bits = bits & ~self._exact_sub_mute_mask

            async def exchange() -> None:
                try:
                    if control_bits:
                        await self._control(f"mute {control_bits} 0", reply=True)
                    self._output_mask &= ~bits
                finally:
                    if input_bits:
                        await self._control(f"inmute {input_bits} 0", reply=True)
                        self._input_mask &= ~input_bits

            await run_to_completion(exchange())

    async def reset_output_peaks(self) -> None:
        await self._control("peaks reset", reply=True)

    async def read_output_peaks(self) -> dict[str, float]:
        payload = json.loads(await self._control("peaks get", reply=True))
        values = payload.get("peaks") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise RuntimeError("Native DSP returned invalid peak data")
        return {f"output_{index + 1}": float(value) for index, value in enumerate(values)}

    async def set_effect_bypass(self, enabled: bool) -> bool:
        async def exchange() -> bool:
            async with self._control_lock:
                previous = bool(int((await self._control_unlocked(
                    "effects bypass get", reply=True)).strip()))
                await self._control_unlocked(
                    f"effects bypass {1 if enabled else 0}", reply=True)
                self._effect_bypass = bool(enabled)
            return previous
        return await run_to_completion(exchange())

    async def enter_raw_measurement(self) -> bool:
        await self._measurement_scope_lock.acquire()
        self._measurement_active = True
        transition = None
        try:
            await run_to_completion(self._suspend_surround_input())
            transition = asyncio.create_task(self.set_effect_bypass(True))
            previous = await asyncio.shield(transition)
            if self._latency_tracking:
                self.freeze_measurement_latency(await self.settled_latency())
            return previous
        except BaseException as exc:
            async def restore() -> None:
                try:
                    if transition is not None:
                        previous = await transition
                        await self.set_effect_bypass(previous)
                finally:
                    await self._resume_surround_input()
            try:
                await restore_after(exc, restore(), what='Raw measurement entry restore')
            finally:
                self._measurement_active = False
                self._measurement_scope_lock.release()
            raise

    async def exit_raw_measurement(self, previous: bool) -> None:
        """Restore the pre-measurement bypass state and release the scope.

        The engine exchange and the recorded state finish as one unit: a
        cancel received meanwhile is re-raised only after the restoration
        has landed, so the scope lock is never released while an old
        restore is still in flight and no second measurement path can take
        over the scope early.
        """
        self._measurement_latency_signature = None
        self._measurement_latency_owner = None
        self._measurement_peak_capture_id = None
        self._measurement_latency_snapshot.pop('ingress_restored', None)
        restoration = asyncio.create_task(self.set_effect_bypass(previous))
        bypass_restored = False
        try:
            await run_to_completion(restoration)
            bypass_restored = True
        finally:
            try:
                await run_to_completion(self._resume_surround_input())
                if bypass_restored:
                    await run_to_completion(self._confirm_measurement_ingress_restore())
            finally:
                self._measurement_active = False
                self._measurement_scope_lock.release()

    async def enter_active_measurement(self) -> bool:
        await self._measurement_scope_lock.acquire()
        self._measurement_active = True
        try:
            await run_to_completion(self._suspend_surround_input())
            if self._latency_tracking:
                self.freeze_measurement_latency(await self.settled_latency())
        except BaseException as exc:
            try:
                await restore_after(exc, self._resume_surround_input(), what='Active measurement entry restore')
            finally:
                self._measurement_active = False
                self._measurement_scope_lock.release()
            raise
        return self._effect_bypass

    async def exit_active_measurement(self, _previous: bool) -> None:
        self._measurement_latency_signature = None
        self._measurement_latency_owner = None
        self._measurement_peak_capture_id = None
        self._measurement_latency_snapshot.pop('ingress_restored', None)
        try:
            await run_to_completion(self._resume_surround_input())
            await run_to_completion(self._confirm_measurement_ingress_restore())
        finally:
            self._measurement_active = False
            self._measurement_scope_lock.release()

    async def _suspend_surround_input(self) -> None:
        self._measurement_peak_capture_id = None
        self._measurement_latency_snapshot.pop('ingress_restored', None)
        self._measurement_ingress_restore = {}
        if self._latency_tracking and (self._surround_input or self._mapped_ingress):
            entry = await self.settled_latency()
            self._measurement_ingress_restore = {
                'input': entry['input'], 'surround_input': self._surround_input,
                'mapped_ingress': [list(edge) for edge in self._mapped_ingress]}
        self._measurement_surround_input = self._surround_input
        self._measurement_mapped_ingress = self._mapped_ingress
        if self._surround_input:
            await self.set_surround_input(False)
        if self._mapped_ingress:
            await self.set_mapped_ingress(())

    async def _resume_surround_input(self) -> None:
        if self._measurement_surround_input:
            await self.set_surround_input(True)
            self._measurement_surround_input = False
        if self._measurement_mapped_ingress:
            mapped, self._measurement_mapped_ingress = self._measurement_mapped_ingress, ()
            await self.set_mapped_ingress(mapped)

    async def _confirm_measurement_ingress_restore(self) -> None:
        """Certify the actual restored graph and input, never routing intent alone."""
        capture = self._measurement_latency_snapshot
        ingress = self._measurement_ingress_restore
        if not ingress or capture.get('verified') is not True:
            return
        engine_inputs = {f'{DSP_NODE_NAME}:input_{index + 1}'
                         for index in range(self._config.input_count)}
        expected = [(source, target) for source, target in ingress['mapped_ingress']
                    if target in engine_inputs]
        if ingress['surround_input']:
            expected.extend(decoded_ingress_edges(self._config.input_count))
        links = await self._run(('pw-link', '-l'))
        if (links.returncode or any(not _contains_link(links.stdout, source, target)
                                    for source, target in expected)):
            raise RuntimeError('Measurement ingress restoration links are unavailable')
        restored = await self.settled_latency()
        if (restored.get('input') != ingress['input']
                or capture.get('ingress_restore') != ingress):
            raise RuntimeError('Measurement ingress restoration latency changed')
        capture['ingress_restored'] = True

    async def set_output_gain_db(self, gain_db: float) -> float:
        value = max(-80.0, min(0.0, float(gain_db)))
        await self._control(f"gain db {value:.9g}", reply=True)
        self._output_gain_db = value
        return value

    async def read_output_gain_db(self) -> float:
        """Return the gain the engine really renders, and adopt it as ours.

        ``gain db get`` is the engine's own readback.  Without it the recorded
        gain can only ever echo what this process last sent, so a transition
        that failed between its duck and its ramp would leave the guard behind
        as the level every later transition reads as the operating gain.
        """
        reply = (await self._control("gain db get", reply=True)).strip()
        try:
            value = float(reply)
        except ValueError as exc:
            raise RuntimeError(
                f"Native DSP gain readback is not a number: {reply!r}") from exc
        if not math.isfinite(value) or not -80.0 <= value <= 0.0:
            raise RuntimeError(
                f"Native DSP gain readback is out of range: {reply!r}")
        recorded = self._output_gain_db
        if (type(recorded) not in (int, float) or not math.isfinite(recorded)
                or abs(float(recorded) - value) > OUTPUT_GAIN_TOLERANCE_DB):
            logger.warning(
                "Native DSP engine gain %.9g dB differs from the recorded %s; "
                "adopting the engine value", value, recorded)
        self._output_gain_db = value
        return value

    async def ramp_output_gain_db(self, start_db: float, target_db: float, *,
                                  step_db: float = 3.0,
                                  interval_seconds: float = 0.006) -> None:
        current = max(-80.0, min(0.0, float(start_db)))
        target = max(-80.0, min(0.0, float(target_db)))
        await self.set_output_gain_db(current)
        while current < target:
            current = min(target, current + max(0.1, float(step_db)))
            if interval_seconds > 0:
                await asyncio.sleep(interval_seconds)
            await self.set_output_gain_db(current)

    async def _control(self, command: str, *, reply: bool) -> str:
        async with self._control_lock:
            return await self._control_unlocked(command, reply=reply)

    async def _control_unlocked(self, command: str, *, reply: bool) -> str:
        self._require_control_state()
        if self._control_socket is None or self._control_path is None:
            raise RuntimeError("Native DSP control is unavailable")
        try:
            self._control_socket.sendto(command.encode(), str(self._control_path))
            if not reply:
                return ""
            loop = asyncio.get_running_loop()
            data = await asyncio.wait_for(
                loop.sock_recv(self._control_socket, CONTROL_REPLY_MAX_BYTES + 1),
                CONTROL_TIMEOUT_SECONDS,
            )
        except (OSError, asyncio.TimeoutError, asyncio.CancelledError):
            self._poison_control()
            raise
        if len(data) > CONTROL_REPLY_MAX_BYTES:
            self._poison_control()
            raise RuntimeError(
                "Native DSP control reply exceeded the receive buffer and was truncated"
            )
        response = data.decode(errors="replace")
        if response.startswith("error"):
            if command == "live commit":
                self._poison_control()
            raise RuntimeError(response)
        if command not in {"peaks get", "gain db get", "effects bypass get", "latency get"} and response != "ok\n":
            self._poison_control()
            raise RuntimeError(f"Native DSP returned an unexpected control reply: {response!r}")
        return response

    def _poison_control(self) -> None:
        """Retire the reply socket; no later command can consume its old ACK."""
        self._control_poisoned = True
        self._error = "Native DSP control state is unknown; rebuild the engine"
        if self._control_socket is not None:
            self._control_socket.close()

    async def _operating_output_gain_db(self) -> float:
        """The level in effect before a transition ducks the output.

        Read from the engine while it answers: the return level has to be the
        gain the output really carries, not this process's record of what it
        last sent, or a guard left behind by an earlier failed transition would
        be ramped back to as if it were the operating level.
        """
        if self._control_socket is None:
            return max(-80.0, min(0.0, float(self._output_gain_db)))
        return await self.read_output_gain_db()

    def _guard_return_gain_db(self, requested_db: float,
                              operating_gain_db: float, name: str) -> float:
        """Return the level a transition path should leave the output on.

        The guard exists to keep the output quiet during the graph swap, so the
        duck must not outlive the transition: a requested target below the gain
        the engine already had is a stale reading (typically a caller that read
        the guard back as the operating gain), and following it would pin the
        output low for good.  The higher of the two wins.
        """
        requested = float(requested_db)
        if requested < operating_gain_db - OUTPUT_GAIN_TOLERANCE_DB:
            logger.warning(
                "Native DSP %s %.9g dB is below the engine's operating gain %.9g dB; "
                "returning to the operating gain", name, requested, operating_gain_db)
            return operating_gain_db
        return requested

    async def _settle_output_gain_db(self, target_db: float) -> None:
        """Put the output back on this path's return level and let the engine confirm it.

        Runs on every exit of a guarded transition, success or failure, so the
        duck can neither survive a failed rollback nor go unnoticed.  Never
        raises: the caller may already be unwinding an error.
        """
        if self._control_socket is None:
            self._output_gain_db = float(target_db)
            return
        try:
            await self.set_output_gain_db(target_db)
            actual = await self.read_output_gain_db()
        except Exception:
            logger.exception(
                "Native DSP operating gain %.9g dB could not be confirmed by the engine",
                target_db)
            return
        if abs(actual - float(target_db)) > OUTPUT_GAIN_TOLERANCE_DB:
            logger.warning(
                "Native DSP operating gain restore did not land: requested %.9g dB, "
                "engine reports %.9g dB", target_db, actual)

    async def _settle_output_gain_db_cancellation_safe(self, target_db: float) -> None:
        """Restore the operating gain even while this task is being cancelled.

        A cancellation that skipped the restore would leave the output at the
        guard level, which is the state that must not survive.
        """
        settle = asyncio.create_task(self._settle_output_gain_db(target_db))
        while not settle.done():
            try:
                await asyncio.shield(settle)
            except asyncio.CancelledError:
                continue
        settle.result()

    async def guarded_rebuild_rendered(self, new: PlannedSyncTarget, *,
                                       previous: PlannedSyncTarget,
                                       guard_db: float,
                                       apply_candidate: Callable[[], Any],
                                       apply_previous: Callable[[], Any],
                                       settle_seconds: float = 0.35,
                                       before_ramp: Callable[[], Awaitable[Any]] | None = None,
                                       before_rollback_ramp: Callable[[], Awaitable[Any]] | None = None,
                                       ramp_target_db: float = 0.0,
                                       rollback_ramp_target_db: float = 0.0) -> None:
        """Guarded rebuild of a prebuilt plan target, with rollback to previous.

        The previous target is an explicit parameter: no durable source can
        re-derive it, so the caller owns both documents.  ``ramp_target_db`` / ``rollback_ramp_target_db``
        bound the post-transition operating gain (default 0 dB); a rollback
        may restore a different gain than the candidate path intended.  Neither
        may leave the output below the gain the engine reported before the
        duck: that level is read back from the engine rather than taken from
        this process's record, and every exit path returns to the higher of the
        two and verifies it through the engine again.
        """
        for name, target in (("ramp_target_db", ramp_target_db),
                             ("rollback_ramp_target_db", rollback_ramp_target_db)):
            if (type(target) not in (int, float) or not math.isfinite(target)
                    or not -80 <= target <= 0):
                raise ValueError(f"{name} must be finite and between -80 and 0 dB")
        guard = min(max(-80.0, min(0.0, float(guard_db))),
                    float(ramp_target_db), float(rollback_ramp_target_db))
        hot_update = self._can_hot_update(new.config)
        settle_seconds = 0.0 if hot_update else settle_seconds
        async with self._measurement_scope_lock:
            # Read the real level before the duck; every exit path returns to it.
            operating_gain = await self._operating_output_gain_db()
            ramp_return = self._guard_return_gain_db(
                ramp_target_db, operating_gain, "ramp_target_db")
            rollback_return = self._guard_return_gain_db(
                rollback_ramp_target_db, operating_gain, "rollback_ramp_target_db")
            if self._control_socket is not None:
                await self.set_output_gain_db(guard)
            try:
                async with self._lock:
                    await self._run_sync(new.config, new.text,
                                         initial_output_gain_db=guard,
                                         **({"validate_current": new.validate_current}
                                            if new.validate_current else {}))
                if settle_seconds > 0:
                    await asyncio.sleep(settle_seconds)
                if before_ramp:
                    await before_ramp()
                await self.ramp_output_gain_db(guard, ramp_return)
                await self._settle_output_gain_db(ramp_return)
                apply_candidate()
                if new.on_applied:
                    new.on_applied()
            except BaseException:
                rollback_applied = False
                try:
                    apply_previous()
                    rollback_applied = True
                    async with self._lock:
                        await self._run_sync(previous.config, previous.text,
                                             initial_output_gain_db=guard,
                                             **({"validate_current": previous.validate_current}
                                                if previous.validate_current else {}))
                    if settle_seconds > 0:
                        await asyncio.sleep(settle_seconds)
                    if before_rollback_ramp:
                        await before_rollback_ramp()
                    await self.ramp_output_gain_db(guard, rollback_return)
                    if previous.on_applied:
                        previous.on_applied()
                except BaseException:
                    logger.exception("Native DSP guarded transition rollback failed")
                finally:
                    # A vetoed rollback (apply_previous raised) leaves the state
                    # to its newer owner, including this gain.
                    if rollback_applied:
                        await self._settle_output_gain_db_cancellation_safe(rollback_return)
                raise

    async def sync_rendered(self, target: PlannedSyncTarget, *,
                            initial_output_gain_db: float = 0.0) -> None:
        """Sync a prebuilt plan target under the measurement scope and graph locks."""
        if not target.config.output_key:
            raise RuntimeError("Native DSP requires a selected hardware output")
        async with self._measurement_scope_lock:
            async with self._lock:
                await self._run_sync(target.config, target.text,
                                     initial_output_gain_db=initial_output_gain_db,
                                     **({"validate_current": target.validate_current}
                                        if target.validate_current else {}))
                if target.on_applied:
                    target.on_applied()

    async def fail_closed(self, error: str, *, output_key: str = "") -> None:
        """Mute the hardware and retire any unsafe helper; retain a visible error."""
        async with self._measurement_scope_lock:
            async with self._lock:
                try:
                    if output_key:
                        result = await self._run(("pactl", "set-sink-mute", output_key, "1"))
                        if result.returncode:
                            raise RuntimeError(result.stderr or "Cannot mute hardware output")
                        result = await self._run(("pactl", "get-sink-mute", output_key))
                        if result.returncode or result.stdout.strip().lower() != "mute: yes":
                            raise RuntimeError("Hardware output mute could not be confirmed")
                finally:
                    try:
                        await self.stop_verified()
                        await self._remove_direct_source_links()
                    finally:
                        self._error = error

    async def _run_sync(self, config: DSPRuntimeConfig, text: str, *,
                        initial_output_gain_db: float = 0.0,
                        validate_current: Callable[[], None] | None = None) -> None:
        """Preflight and commit one (config, text) pair; caller holds _lock."""
        if not self.binary.is_file():
            self._error = f"Native DSP binary is not available: {self.binary}"
            raise RuntimeError(self._error)
        self._latency_snapshot = {}
        self._last_input_latency_ms = None
        if self._latency_tracking:
            input_ms = await self._input_latency_ms()
            text += f'input_latency {input_ms:.9g}\n'
        await self._stop_orphan_helpers()
        config_name = await self._preflight_text(text)
        try:
            if validate_current:
                validate_current()
        except BaseException:
            Path(config_name).unlink(missing_ok=True)
            raise

        if self._can_hot_update(config):
            try:
                if await self._try_live_update(text):
                    await self._reconcile_output_links(config)
                    Path(config_name).unlink(missing_ok=True)
                    self._config = config
                    self._config_text = text
                    self._effect_bypass = any(line.strip() == 'bypass 1' for line in text.splitlines())
                    self._error = None
                    return
                if self._control_poisoned:
                    raise RuntimeError("Native DSP live update state is unknown; rebuilding engine")
                await self._control(
                    f"swap config {config_name} {max(-80.0, min(0.0, float(initial_output_gain_db))):.9g}",
                    reply=True,
                )
                await self._reconcile_output_links(config)
                Path(config_name).unlink(missing_ok=True)
                self._config = config
                self._config_text = text
                self._effect_bypass = any(line.strip() == 'bypass 1' for line in text.splitlines())
                self._error = None
                return
            except Exception as exc:
                logger.warning("Native DSP hot update failed; falling back to process rebuild: %s", exc)

        await self.stop()
        self._config_path = Path(config_name)
        try:
            control_dir = Path(tempfile.mkdtemp(prefix="fxroute-dsp-control-"))
            self._control_path = control_dir / "engine.sock"
            self._control_client_path = control_dir / "client.sock"
            self._control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self._control_socket.setblocking(False)
            self._control_socket.bind(str(self._control_client_path))
            self._process = await self._launch((str(self.binary), config_name, str(self._control_path)))
            self._config = config
            self._config_text = text
            self._started_at = time.time()
            self._start_stderr_drain(self._process)
            await self._wait_for_ports(config)
            await self.set_output_gain_db(initial_output_gain_db)
            self._effect_bypass = bool(int(
                (await self._control("effects bypass get", reply=True)).strip()))
            await self._remove_direct_source_links()
            await self._reconcile_ingress_links()
            await self._reconcile_output_links(config)
            self._error = None
        except Exception as exc:
            try:
                await self.stop()
            finally:
                self._error = str(exc)
            raise

    async def preflight_rendered(self, target: PlannedSyncTarget) -> None:
        """Validate a target without touching the running helper or its links."""
        path = await self._preflight_text(target.text)
        Path(path).unlink(missing_ok=True)

    async def _preflight_text(self, text: str) -> str:
        if not self.binary.is_file():
            raise RuntimeError(f"Native DSP binary is not available: {self.binary}")
        fd, config_name = tempfile.mkstemp(prefix="fxroute-dsp-", suffix=".conf")
        os.write(fd, text.encode("utf-8")); os.close(fd)
        input_fd, input_name = tempfile.mkstemp(prefix="fxroute-dsp-input-", suffix=".f32")
        output_fd, output_name = tempfile.mkstemp(prefix="fxroute-dsp-output-", suffix=".f32")
        os.close(input_fd); os.close(output_fd)
        try:
            offline_binary = self.binary.with_name(f"{self.binary.name}-offline")
            result = await self._run((str(offline_binary), config_name, input_name, output_name))
            if result.returncode:
                raise RuntimeError(result.stderr or result.stdout or "Native DSP preflight failed")
            return config_name
        except BaseException:
            Path(config_name).unlink(missing_ok=True)
            raise
        finally:
            Path(input_name).unlink(missing_ok=True)
            Path(output_name).unlink(missing_ok=True)

    async def _wait_for_ports(self, config: DSPRuntimeConfig) -> None:
        expected = [f"{DSP_NODE_NAME}:input_{index + 1}" for index in range(config.input_count)]
        expected.extend(f"{DSP_NODE_NAME}:output_{index + 1}" for index in range(len(config.layout)))
        expected.extend(f"{DSP_NODE_NAME}:{port}" for port in DSP_POST_EFFECT_PORTS)
        for _ in range(50):
            result = await self._run(("pw-link", "-io"))
            if result.returncode == 0 and all(port in result.stdout for port in expected):
                return
            if self._process is not None and getattr(self._process, "returncode", None) is not None:
                break
            await asyncio.sleep(.1)
        detail = self.stderr_tail().strip()
        raise RuntimeError(
            "Native DSP did not expose expected PipeWire ports"
            + (f"; engine stderr: {detail}" if detail else "")
        )

    def _can_hot_update(self, config: DSPRuntimeConfig) -> bool:
        """Return whether the prepared state fits the existing native node."""
        return bool(
            self._process is not None
            and getattr(self._process, "returncode", None) is None
            and self._control_socket is not None
            and not self._control_poisoned
            and self._config is not None
            and self._config.output_key == config.output_key
            and self._config.sample_rate == config.sample_rate
            and len(self._config.layout) == len(config.layout)
            and self._config.input_count == config.input_count
        )

    @staticmethod
    def _live_config(text: str) -> tuple[list[tuple], list[tuple]]:
        """Return immutable stage shape and mutable values from a native config."""
        shape = []
        values = []
        stage = None
        peq_indices = {}
        for raw in text.splitlines():
            parts = shlex.split(raw)
            if not parts:
                continue
            kind = parts[0]
            if kind == "stage_begin":
                stage = parts[2]
                shape.append((kind, parts[2], *parts[3:]))
            elif kind == "stage_end":
                stage = None
            elif kind == "control" and stage is not None and len(parts) == 3:
                shape.append((kind, stage, parts[1]))
                values.append((kind, stage, parts[1], float(parts[2])))
            elif kind == "param" and stage is not None and len(parts) >= 3:
                if parts[1] in {"path", "fft_size"}:
                    # Paths and transform sizes cannot change live: a new value rebuilds the engine.
                    shape.append((kind, stage, parts[1], *parts[2:]))
                else:
                    shape.append((kind, stage, parts[1]))
                    try:
                        values.append((kind, stage, parts[1], float(parts[2])))
                    except ValueError:
                        shape.append(("static", *parts[1:]))
            elif kind == "matrix" and len(parts) == 4:
                shape.append((kind, parts[1], parts[2]))
                values.append((kind, int(parts[1]), int(parts[2]), float(parts[3])))
            elif kind == "peq" and len(parts) == 6:
                index = peq_indices.get(parts[1], 0)
                peq_indices[parts[1]] = index + 1
                shape.append((kind, parts[1], index, parts[2]))
                values.append((kind, int(parts[1]), index, parts[2], *(float(item) for item in parts[3:])))
            elif kind == "output" and len(parts) == 5:
                shape.append((kind, parts[1]))
                values.append((kind, int(parts[1]), float(parts[2]), float(parts[3]), parts[4]))
            elif kind == 'avsync' and len(parts) == 3:
                shape.append((kind, parts[2]))
                values.append((kind, float(parts[1]), int(parts[2])))
            elif kind in {'latency_basis', 'source_basis', 'olatency', 'latency'}:
                shape.append((kind, *parts[1:]))
            elif kind in {"sos", "oconv", "xsos", "gxsos", "gmatrix", "postmatrix", "inputs", "surround"} and len(parts) >= 2:
                # Output-bank lines join the shape with full content: any
                # bank change forces a rebuild, while identical banks allow
                # live updates of unrelated parameters. Live updates *for*
                # bank coefficients arrive with the transaction integration;
                # note peq live indices count peq lines per output, while sos
                # lines share the native filter array in file order.
                shape.append((kind, *parts[1:]))
            elif kind == "bypass":
                shape.append((kind, parts[1:]))
        return shape, values

    async def _try_live_update(self, text: str) -> bool:
        if self._config_text is None:
            return False
        old_shape, old_values = self._live_config(self._config_text)
        new_shape, new_values = self._live_config(text)
        if old_shape != new_shape:
            return False
        old_map = {item[:3]: item[3:] for item in old_values if item[0] in {"control", "param"}}
        updates = []
        for item in new_values:
            key = item[:3]
            if item[0] in {"control", "param"}:
                if old_map.get(key) != item[3:]:
                    updates.append("live %s %s %s %.9g" % (item[0], item[1], item[2], item[3]))
            elif item[0] == "matrix":
                old = next((v for v in old_values if v[:3] == item[:3]), None)
                if old is None or old[3] != item[3]:
                    updates.append(f"live matrix {item[1]} {item[2]} {item[3]:.9g}")
            elif item[0] == "peq":
                old = next((v for v in old_values if v[:3] == item[:3]), None)
                if old is None or old[3:] != item[3:]:
                    updates.append("live peq %s %s %s %.9g %.9g %.9g" % item[1:])
            elif item[0] == "output":
                old = next((v for v in old_values if v[:2] == item[:2]), None)
                if old is None or old[2:] != item[2:]:
                    updates.append("live output %s %.9g %.9g %s" % item[1:])
            elif item[0] == 'avsync':
                old = next((v for v in old_values if v[0] == 'avsync'), None)
                if old != item:
                    updates.append('live avsync %.9g %s' % item[1:])
        if not updates:
            return True
        began = False
        committing = False
        try:
            await self._control("live begin", reply=True)
            began = True
            for command in updates:
                await self._control(command, reply=True)
            committing = True
            await self._control("live commit", reply=True)
            return True
        except BaseException as exc:
            if committing:
                # A failed commit may still apply on the audio thread later.
                self._poison_control()
            elif began and not self._control_poisoned:
                try:
                    await run_to_completion(self._control("live abort", reply=True))
                except BaseException:
                    self._poison_control()
                    logger.exception("Native DSP live abort failed; rebuilding engine")
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.warning("Native DSP live update unavailable; using atomic config swap: %s", exc)
            return False

    async def _reconcile_output_links(self, config: DSPRuntimeConfig) -> None:
        """Reconcile live hardware edges, including links created by other clients."""
        desired = [
            PipeWireLink(f"{DSP_NODE_NAME}:output_{signal}", hardware_link_target(config.output_key, port))
            for signal, port in config_route_pairs(config)
        ]
        result = await self._run(("pw-link", "-l"))
        if result.returncode:
            raise RuntimeError(result.stderr or "Cannot inspect DSP output links")
        live = physical_output_links(result.stdout)
        for link in live - set(desired):
            result = await self._run(("pw-link", "-d", link.source, link.target))
            if result.returncode:
                # A disappearing device may already have removed this edge.
                readback = await self._run(("pw-link", "-l"))
                if readback.returncode or link in physical_output_links(readback.stdout):
                    raise RuntimeError(result.stderr or "Cannot remove stale DSP output link")
            logger.info("Removed stale DSP output link: %s -> %s", link.source, link.target)
        for link in desired:
            if link not in live:
                result = await self._run(("pw-link", link.source, link.target))
                if result.returncode and "exists" not in (result.stderr or "").lower():
                    raise RuntimeError(result.stderr or f"Failed to link {link.source} -> {link.target}")
        self._links = [link for link in self._links if not link.source.startswith(f"{DSP_NODE_NAME}:output_")] + desired

    async def set_surround_input(self, enabled: bool) -> None:
        """Select decoded ingress while retaining app/measurement stereo taps."""
        self._surround_input = bool(enabled)
        if self._engine_running():
            async with self._lock:
                await self._reconcile_ingress_links()
                await self.update_input_latency()

    async def set_mapped_ingress(self, edges: Sequence[tuple[str, str]]) -> None:
        """Select analog multichannel ingress (custom input mapping); empty clears it.

        ``edges`` are (capture port, engine input port) pairs from
        ``mapped_ingress_edges``. The sink monitor stays linked to inputs 1/2.
        """
        self._mapped_ingress = tuple((str(source), str(target)) for source, target in edges)
        if self._engine_running():
            async with self._lock:
                await self._reconcile_ingress_links(strict_mapped=True)

    async def release_mapped_ingress(self, edges: Sequence[tuple[str, str]]) -> None:
        """Clear the mapped ingress only while it is still ``edges``.

        A multichannel stream that ends must not drop the ingress a newer
        owner (another input or a measurement resume) has installed since.
        """
        released = tuple((str(source), str(target)) for source, target in edges)
        if self._measurement_mapped_ingress == released:
            self._measurement_mapped_ingress = ()
        if self._mapped_ingress == released:
            await self.set_mapped_ingress(())

    async def _reconcile_ingress_links(self, *, strict_mapped: bool = False) -> None:
        count = self._config.input_count if self._config else 2
        desired = [PipeWireLink(f"{DSP_INGRESS_MONITOR_NODE}:{port}", f"{DSP_NODE_NAME}:input_{index + 1}")
                   for index, port in enumerate(DSP_INGRESS_PORTS)]
        if self._surround_input:
            desired.extend(PipeWireLink(source, target) for source, target in decoded_ingress_edges(count))
        engine_inputs = {f"{DSP_NODE_NAME}:input_{index + 1}" for index in range(count)}
        mapped = {PipeWireLink(source, target) for source, target in self._mapped_ingress
                  if target in engine_inputs}
        desired.extend(sorted(mapped, key=lambda link: link.target))
        previously_mapped = {link for link in self._links
                             if link.target.startswith(f"{DSP_NODE_NAME}:input_")
                             and not link.source.startswith(f"{DSP_INGRESS_MONITOR_NODE}:")
                             and not link.source.startswith("fxroute_surround_input:")}
        result = await self._run(("pw-link", "-l"))
        if result.returncode:
            raise RuntimeError(result.stderr or "Cannot inspect DSP ingress links")
        live = {PipeWireLink(source, target) for source, target in iter_pw_links(result.stdout)
                if target.startswith(f"{DSP_NODE_NAME}:input_")
                and (source.startswith("fxroute_surround_input:")
                     or PipeWireLink(source, target) in previously_mapped)}
        for link in live - set(desired):
            disconnected = await self._run(("pw-link", "-d", link.source, link.target))
            if disconnected.returncode:
                readback = await self._run(("pw-link", "-l"))
                if readback.returncode or _contains_link(readback.stdout, link.source, link.target):
                    raise RuntimeError(disconnected.stderr or "Cannot disconnect decoded ingress")
        unlinked: set[PipeWireLink] = set()
        for link in desired:
            if not _contains_link(result.stdout, link.source, link.target):
                connected = await self._run(("pw-link", link.source, link.target))
                if connected.returncode and "exists" not in connected.stderr.lower():
                    if link in mapped and not strict_mapped:
                        # A missing capture node must not fail an engine rebuild;
                        # the external-input monitor re-links it once it returns.
                        logger.warning("Mapped ingress link %s -> %s not established: %s",
                                       link.source, link.target, (connected.stderr or "").strip())
                        unlinked.add(link)
                        continue
                    raise RuntimeError(connected.stderr or "Cannot connect DSP ingress")
        established = [link for link in desired if link not in unlinked]
        if self._surround_input or mapped or live:
            readback = await self._run(("pw-link", "-l"))
            if (readback.returncode
                    or any(not _contains_link(readback.stdout, link.source, link.target) for link in established)
                    or any(_contains_link(readback.stdout, link.source, link.target) for link in live - set(desired))):
                raise RuntimeError("Decoded ingress links could not be verified")
        self._links = [link for link in self._links if not link.target.startswith(f"{DSP_NODE_NAME}:input_")] + established

    async def stop_verified(self) -> None:
        """Require helper exit; retain its identity if emergency shutdown fails."""
        process = self._process
        links = list(self._links)
        try:
            await self.stop()
        finally:
            # stop() may finish cleanup after both reap timeouts. Its cleared
            # snapshot alone is not evidence that the captured helper exited.
            if process is not None and getattr(process, "returncode", None) is None:
                self._process = process
                self._links = links
                self._error = "Native DSP helper exit could not be confirmed; recovery required"
                raise RuntimeError(self._error)

    async def stop(self) -> None:
        for link in self._links:
            await self._run(("pw-link", "-d", link.source, link.target))
        self._links = []
        process, self._process = self._process, None
        # Keep the stderr drain running while the process is terminating: a
        # cancelled drain could let the engine fill the pipe during shutdown.
        # The drain is stopped/joined only after the process exited or was
        # killed and reaped.
        if process is not None and getattr(process, "returncode", None) is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    logger.warning("Native DSP process did not exit after SIGKILL; drain stopped without reap")
        await self._stop_stderr_drain()
        if self._config_path:
            self._config_path.unlink(missing_ok=True)
            self._config_path = None
        control_socket, self._control_socket = self._control_socket, None
        if control_socket is not None:
            control_socket.close()
        control_dir = self._control_path.parent if self._control_path else None
        for path in (self._control_path, self._control_client_path):
            if path:
                path.unlink(missing_ok=True)
        if control_dir:
            try:
                control_dir.rmdir()
            except OSError:
                pass
        self._control_path = self._control_client_path = None
        self._control_poisoned = False
        self._exact_sub_mute = False
        self._exact_sub_mute_mask = 0
        self._effect_bypass = False
        self._output_gain_db = 0.0
        self._output_mask = 0
        self._input_mask = 0
        self._config_text = None

    async def verify(self) -> bool:
        if not self._links:
            return False
        result = await self._run(("pw-link", "-l"))
        desired = {link for link in self._links if link.source.startswith(f"{DSP_NODE_NAME}:output_")}
        return (result.returncode == 0
                and not (physical_output_links(result.stdout) - desired)
                and all(_contains_link(result.stdout, link.source, link.target) for link in self._links))

    def stderr_tail(self) -> str:
        """Bounded engine stderr tail kept by the drain task, for diagnostics."""
        return self._stderr_tail.decode("utf-8", "replace")

    def _start_stderr_drain(self, process: Any) -> None:
        """Own a continuous stderr drain for the running engine process.

        Without a continuous reader a sufficiently large stderr output can
        fill the kernel pipe buffer and block the real-time engine.  Only a
        bounded tail is retained, so the pipe never stalls and RAM stays
        limited.
        """
        stderr = getattr(process, "stderr", None)
        if stderr is None:
            return
        self._stderr_tail = b""
        self._stderr_drain_task = asyncio.create_task(
            self._drain_engine_stderr(),
            name="fxroute-dsp-stderr-drain",
        )

    async def _drain_engine_stderr(self) -> None:
        process = self._process
        stderr = getattr(process, "stderr", None)
        if stderr is None:
            return
        try:
            while True:
                chunk = await stderr.read(4096)
                if not chunk:
                    break
                self._stderr_tail = (self._stderr_tail + chunk)[-HELPER_STDERR_TAIL_LIMIT:]
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to drain native DSP engine stderr")

    async def _stop_stderr_drain(self) -> None:
        task = self._stderr_drain_task
        self._stderr_drain_task = None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.gather(task, return_exceptions=True)
        except asyncio.CancelledError:
            pass

    async def _stop_orphan_helpers(self) -> None:
        """Terminate stale fxroute-dsp engine processes left by a killed service.

        A hard service kill leaves the engine process behind; on restart the
        stale engine would keep its PipeWire ports, so a fresh engine could
        not claim them.  The running runtime's own process (if any) is never
        touched; the caller stops it through the normal stop() path.
        """
        binary = str(self.binary)
        pattern = re.escape(binary) + r"\s"
        own_pids = set()
        process = self._process
        if process is not None and getattr(process, "returncode", None) is None:
            own_pids.add(int(getattr(process, "pid", 0) or 0))
        result = await self._run(("pgrep", "-f", pattern))
        orphan_pids = [int(pid) for pid in result.stdout.split()
                       if pid.isdigit() and int(pid) not in own_pids]
        if not orphan_pids:
            return
        logger.info("Found orphan native DSP processes (pids: %s), cleaning up", ", ".join(map(str, orphan_pids)))
        for pid in orphan_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        await asyncio.sleep(RUNTIME_ORPHAN_KILL_GRACE_SECONDS)
        result = await self._run(("pgrep", "-f", pattern))
        remaining = [int(pid) for pid in result.stdout.split()
                     if pid.isdigit() and int(pid) not in own_pids]
        if remaining:
            logger.warning("Orphan native DSP processes ignored SIGTERM (pids: %s), killing", ", ".join(map(str, remaining)))
            for pid in remaining:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            await asyncio.sleep(RUNTIME_ORPHAN_KILL_GRACE_SECONDS)

    @staticmethod
    async def _run_command(args: Sequence[str]) -> CommandResult:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), RUNTIME_COMMAND_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            # A wedged PipeWire registry must not hold the runtime locks
            # forever.  Terminate, allow a short grace, then kill and fully
            # reap the child so no command survives the call; report the
            # timeout as a command failure exactly like a nonzero exit.
            # If the caller is cancelled while the cleanup drains, the
            # cancellation wins over the timeout failure.
            if await stop_command_child_cancellation_safe(
                process, grace_seconds=RUNTIME_COMMAND_TERMINATE_GRACE_SECONDS
            ):
                raise asyncio.CancelledError
            return CommandResult(
                RUNTIME_COMMAND_TIMEOUT_RETURNCODE,
                "",
                f"Command timed out after {RUNTIME_COMMAND_TIMEOUT_SECONDS}s: {' '.join(args)}",
            )
        except asyncio.CancelledError:
            # Caller cancellation must not leave the child behind; the
            # shielded cleanup terminates/kills/drains it even under further
            # cancellation, then the original cancellation is re-raised so
            # the lock holders release ownership.
            await stop_command_child_cancellation_safe(
                process, grace_seconds=RUNTIME_COMMAND_TERMINATE_GRACE_SECONDS
            )
            raise
        return CommandResult(process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace"))

    async def _launch_process(self, args: Sequence[str]) -> Any:
        launch_args = list(args)
        launcher = self._realtime_launcher
        if launcher and shutil.which(launcher):
            launch_args = [launcher, "-f", "20", *launch_args]
        return await asyncio.create_subprocess_exec(*launch_args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
