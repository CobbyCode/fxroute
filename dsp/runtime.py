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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from audio.pw_link import stop_command_child_cancellation_safe

DSP_NODE_NAME = "fxroute_dsp"
DSP_INGRESS_MONITOR_NODE = "fxroute_dsp_sink"
DSP_INGRESS_PORTS = ("monitor_FL", "monitor_FR")
DSP_INPUT_PORTS = ("input_1", "input_2")
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
CONTROL_REPLY_MAX_BYTES = 4096
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

    @classmethod
    def from_overview(cls, overview: dict[str, Any]) -> "DSPRuntimeConfig":
        bass = BassManagementConfig.from_overview(overview)
        layout = [
            {"name": "FL", "routes": [{"input": 0, "gain": 1.0}]},
            {"name": "FR", "routes": [{"input": 1, "gain": 1.0}]},
        ]
        ports = ["playback_FL", "playback_FR"]
        if bass.output_channels >= 4:
            ports.extend(("playback_RL", "playback_RR"))
        if bass.output_mode.startswith("subwoofer-2."):
            if bass.main_highpass_enabled:
                for channel in layout:
                    channel["filters"] = [{"type": "highpass", "frequency_hz": bass.crossover_frequency_hz, "q": 0.70710678, "stages": 2}]
            for channel in layout:
                channel["delay_ms"] = bass.derived_main_delay_ms
            stereo_bass = bass.bass_routing == "stereo"
            sub_defs = (
                (bass.sub_level_db, bass.derived_sub1_delay_ms, bass.sub_polarity),
                (bass.sub2_level_db, bass.derived_sub2_delay_ms, bass.sub2_polarity),
            )
            for index, (level_db, delay_ms, polarity) in enumerate(sub_defs):
                routes = ([{"input": index, "gain": 1.0}] if stereo_bass else
                          [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}])
                layout.append({
                    "name": f"SUB{index + 1}", "routes": routes,
                    "gain_db": level_db,
                    "delay_ms": delay_ms,
                    "invert": polarity == "invert",
                    "filters": [{"type": "lowpass", "frequency_hz": bass.crossover_frequency_hz, "q": 0.70710678, "stages": 2}],
                })
        else:
            layout.extend((
                {"name": "SUB1", "routes": [{"input": 0, "gain": 0.0}]},
                {"name": "SUB2", "routes": [{"input": 1, "gain": 0.0}]},
            ))
        return cls(bass.output_mode, bass.output_key, bass.sample_rate, tuple(ports), tuple(layout))


class DSPRuntime:
    def __init__(self, manager: Any, *, binary: str | Path | None = None,
                 command_runner: Callable[[Sequence[str]], Awaitable[CommandResult]] | None = None,
                 process_launcher: Callable[[Sequence[str]], Awaitable[Any]] | None = None):
        self.manager = manager
        self.binary = Path(binary or os.environ.get("FXROUTE_DSP_BINARY") or Path(__file__).parent.parent / "native_dsp/build/fxroute-dsp")
        self._run = command_runner or self._run_command
        self._launch = process_launcher or self._launch_process
        self._process = None
        self._config: DSPRuntimeConfig | None = None
        self._config_path: Path | None = None
        self._config_text: str | None = None
        self._links: list[PipeWireLink] = []
        self._lock = asyncio.Lock()
        self._measurement_scope_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()
        self._error: str | None = None
        self._started_at: float | None = None
        self._control_socket: socket.socket | None = None
        self._control_path: Path | None = None
        self._control_client_path: Path | None = None
        self._exact_sub_mute = False
        self._effect_bypass = False
        self._output_gain_db = 0.0
        self._stderr_drain_task: asyncio.Task | None = None
        self._stderr_tail = b""

    @property
    def sync_in_progress(self) -> bool:
        return self._lock.locked()

    def _engine_running(self) -> bool:
        return self._process is not None and getattr(self._process, "returncode", None) is None

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
        for node in ("mpv", "spotify"):
            for channel in ("FL", "FR", "RL", "RR"):
                await self._run((
                    "pw-link", "-d", f"{node}:output_{channel}",
                    f"{self._config.output_key}:playback_{channel}",
                ))

    async def reclean_direct_dsp_links(self) -> None:
        if self._config is not None:
            await self._reconcile_output_links(self._config)
        await self._reclean_guarded()

    def snapshot(self) -> dict[str, Any]:
        running = self._process is not None and getattr(self._process, "returncode", None) is None
        return {"active": running and not self._error and bool(self._links), "engine": "fxroute_native_dsp",
                "helper_pid": getattr(self._process, "pid", None) if running else None,
                "helper_args": [str(self.binary), str(self._config_path)] if self._config_path else None,
                "config": {"sample_rate": self._config.sample_rate, "output_mode": self._config.output_mode,
                            "output_key": self._config.output_key,
                            "layout": [dict(channel) for channel in getattr(self._config, "layout", ())]} if self._config else None,
                "last_error": self._error, "last_started_at": self._started_at,
                "links_configured": bool(self._links), "exact_sub_mute": self._exact_sub_mute,
                "effect_bypass": self._effect_bypass, "output_gain_db": self._output_gain_db,
                "stderr_tail": self.stderr_tail()[:2048]}

    async def set_exact_sub_mute(self, enabled: bool) -> bool:
        previous = self._exact_sub_mute
        if len(self._config.hardware_ports if self._config else ()) < 4:
            raise RuntimeError("Sub outputs are unavailable")
        await self._control(f"mute 12 {1 if enabled else 0}", reply=True)
        self._exact_sub_mute = bool(enabled)
        return previous

    async def reset_output_peaks(self) -> None:
        await self._control("peaks reset", reply=True)

    async def read_output_peaks(self) -> dict[str, float]:
        payload = json.loads(await self._control("peaks get", reply=True))
        values = payload.get("peaks") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise RuntimeError("Native DSP returned invalid peak data")
        return {f"output_{index + 1}": float(value) for index, value in enumerate(values)}

    async def set_effect_bypass(self, enabled: bool) -> bool:
        async with self._control_lock:
            previous = bool(int((await self._control_unlocked(
                "effects bypass get", reply=True)).strip()))
            await self._control_unlocked(
                f"effects bypass {1 if enabled else 0}", reply=True)
        self._effect_bypass = bool(enabled)
        return previous

    async def enter_raw_measurement(self) -> bool:
        await self._measurement_scope_lock.acquire()
        transition = asyncio.create_task(self.set_effect_bypass(True))
        try:
            return await asyncio.shield(transition)
        except BaseException:
            try:
                previous = await transition
                await asyncio.shield(self.set_effect_bypass(previous))
            finally:
                self._measurement_scope_lock.release()
            raise

    async def exit_raw_measurement(self, previous: bool) -> None:
        restoration = asyncio.create_task(self.set_effect_bypass(previous))
        try:
            await asyncio.shield(restoration)
        except BaseException:
            await restoration
            raise
        finally:
            self._measurement_scope_lock.release()

    async def enter_active_measurement(self) -> bool:
        await self._measurement_scope_lock.acquire()
        return self._effect_bypass

    async def exit_active_measurement(self, _previous: bool) -> None:
        self._measurement_scope_lock.release()

    async def set_output_gain_db(self, gain_db: float) -> float:
        value = max(-80.0, min(0.0, float(gain_db)))
        await self._control(f"gain db {value:.9g}", reply=True)
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
        if self._control_socket is None or self._control_path is None:
            raise RuntimeError("Native DSP control is unavailable")
        self._control_socket.sendto(command.encode(), str(self._control_path))
        if not reply:
            return ""
        loop = asyncio.get_running_loop()
        data = await asyncio.wait_for(
            loop.sock_recv(self._control_socket, CONTROL_REPLY_MAX_BYTES + 1), 1.0
        )
        if len(data) > CONTROL_REPLY_MAX_BYTES:
            raise RuntimeError(
                "Native DSP control reply exceeded the receive buffer and was truncated"
            )
        response = data.decode(errors="replace")
        if response.startswith("error"):
            raise RuntimeError(response)
        return response

    async def guarded_rebuild(self, overview: dict[str, Any], *, guard_db: float,
                              apply_candidate: Callable[[], Any],
                              apply_previous: Callable[[], Any],
                              settle_seconds: float = 0.35,
                              candidate_extras: dict[str, Any] | None = None,
                              previous_extras: dict[str, Any] | None = None,
                              before_ramp: Callable[[], Awaitable[Any]] | None = None,
                              before_rollback_ramp: Callable[[], Awaitable[Any]] | None = None) -> None:
        guard = max(-80.0, min(0.0, float(guard_db)))
        hot_update = self._can_hot_update(DSPRuntimeConfig.from_overview(overview))
        settle_seconds = 0.0 if hot_update else settle_seconds
        async with self._measurement_scope_lock:
            if self._control_socket is not None:
                # Pin a running engine to the guard before the rebuild so it
                # never sits at full gain during the transition.  On the first
                # start no engine exists yet and the rebuild applies the guard
                # through initial_output_gain_db before any audio link exists.
                await self.set_output_gain_db(guard)
            try:
                await self._sync(overview, initial_output_gain_db=guard,
                                 extras_override=candidate_extras)
                if settle_seconds > 0:
                    await asyncio.sleep(settle_seconds)
                if before_ramp:
                    await before_ramp()
                await self.ramp_output_gain_db(guard, 0.0)
                apply_candidate()
            except BaseException:
                try:
                    apply_previous()
                    await self._sync(overview, initial_output_gain_db=guard,
                                     extras_override=previous_extras)
                    if settle_seconds > 0:
                        await asyncio.sleep(settle_seconds)
                    if before_rollback_ramp:
                        await before_rollback_ramp()
                    await self.ramp_output_gain_db(guard, 0.0)
                except BaseException:
                    logger.exception("Native DSP guarded transition rollback failed")
                raise

    async def sync(self, overview: dict[str, Any], *, initial_output_gain_db: float = 0.0,
                   extras_override: dict[str, Any] | None = None) -> None:
        async with self._measurement_scope_lock:
            await self._sync(overview, initial_output_gain_db=initial_output_gain_db,
                             extras_override=extras_override)

    async def _sync(self, overview: dict[str, Any], *, initial_output_gain_db: float = 0.0,
                    extras_override: dict[str, Any] | None = None) -> None:
        config = DSPRuntimeConfig.from_overview(overview)
        if not config.output_key:
            raise RuntimeError("Native DSP requires a selected hardware output")
        async with self._lock:
            if not self.binary.is_file():
                self._error = f"Native DSP binary is not available: {self.binary}"
                raise RuntimeError(self._error)
            await self._stop_orphan_helpers()
            text = self.manager.compile_engine_text(
                list(config.layout), sample_rate_hz=config.sample_rate,
                extras_override=extras_override)
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
            except Exception:
                Path(config_name).unlink(missing_ok=True)
                raise
            finally:
                Path(input_name).unlink(missing_ok=True)
                Path(output_name).unlink(missing_ok=True)

            if self._can_hot_update(config):
                try:
                    if await self._try_live_update(text):
                        Path(config_name).unlink(missing_ok=True)
                        self._config = config
                        self._config_text = text
                        self._error = None
                        return
                    await self._control(
                        f"swap config {config_name} {max(-80.0, min(0.0, float(initial_output_gain_db))):.9g}",
                        reply=True,
                    )
                    await self._reconcile_output_links(config)
                    Path(config_name).unlink(missing_ok=True)
                    self._config = config
                    self._config_text = text
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
                links = [
                    PipeWireLink(f"{DSP_INGRESS_MONITOR_NODE}:{DSP_INGRESS_PORTS[0]}", f"{DSP_NODE_NAME}:{DSP_INPUT_PORTS[0]}"),
                    PipeWireLink(f"{DSP_INGRESS_MONITOR_NODE}:{DSP_INGRESS_PORTS[1]}", f"{DSP_NODE_NAME}:{DSP_INPUT_PORTS[1]}"),
                ]
                links.extend(PipeWireLink(f"{DSP_NODE_NAME}:output_{index + 1}", f"{config.output_key}:{port}")
                             for index, port in enumerate(config.hardware_ports))
                self._links = links
                for link in links:
                    result = await self._run(("pw-link", link.source, link.target))
                    if result.returncode and "exists" not in (result.stderr or "").lower():
                        raise RuntimeError(result.stderr or f"Failed to link {link.source} -> {link.target}")
                self._error = None
            except Exception as exc:
                try:
                    await self.stop()
                finally:
                    self._error = str(exc)
                raise

    async def _wait_for_ports(self, config: DSPRuntimeConfig) -> None:
        expected = [f"{DSP_NODE_NAME}:input_1", f"{DSP_NODE_NAME}:input_2"]
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
            and self._config is not None
            and self._config.output_key == config.output_key
            and self._config.sample_rate == config.sample_rate
            and len(self._config.layout) == len(config.layout)
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
                if parts[1] == "path":
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
        if not updates:
            return True
        try:
            await self._control("live begin", reply=True)
            for command in updates:
                await self._control(command, reply=True)
            await self._control("live commit", reply=True)
            return True
        except Exception as exc:
            logger.warning("Native DSP live update unavailable; using atomic config swap: %s", exc)
            return False

    async def _reconcile_output_links(self, config: DSPRuntimeConfig) -> None:
        """Update only hardware links; native ports remain stable across modes."""
        desired = [
            PipeWireLink(f"{DSP_NODE_NAME}:output_{index + 1}", f"{config.output_key}:{port}")
            for index, port in enumerate(config.hardware_ports)
        ]
        for link in tuple(self._links):
            if link.source.startswith(f"{DSP_NODE_NAME}:output_") and link not in desired:
                await self._run(("pw-link", "-d", link.source, link.target))
                self._links.remove(link)
        for link in desired:
            if link not in self._links:
                result = await self._run(("pw-link", link.source, link.target))
                if result.returncode and "exists" not in (result.stderr or "").lower():
                    raise RuntimeError(result.stderr or f"Failed to link {link.source} -> {link.target}")
                self._links.append(link)
        self._links = [link for link in self._links if not link.source.startswith(f"{DSP_NODE_NAME}:output_")] + desired

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
        self._exact_sub_mute = False
        self._effect_bypass = False
        self._output_gain_db = 0.0
        self._config_text = None

    async def verify(self) -> bool:
        if not self._links:
            return False
        result = await self._run(("pw-link", "-l"))
        return result.returncode == 0 and all(_contains_link(result.stdout, link.source, link.target) for link in self._links)

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

    @staticmethod
    async def _launch_process(args: Sequence[str]) -> Any:
        return await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
