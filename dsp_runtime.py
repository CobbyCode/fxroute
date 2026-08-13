# SPDX-License-Identifier: AGPL-3.0-only
"""Native FXRoute DSP process and PipeWire graph ownership."""

from __future__ import annotations

import asyncio
import math
import os
import tempfile
import time
import json
import logging
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

DSP_NODE_NAME = "fxroute_dsp"
DSP_INGRESS_MONITOR_NODE = "fxroute_dsp_sink"
DSP_INGRESS_PORTS = ("monitor_FL", "monitor_FR")
DSP_INPUT_PORTS = ("input_1", "input_2")
DSP_POST_EFFECT_PORTS = ("post_effect_FL", "post_effect_FR")
DEFAULT_SAMPLE_RATE = 48_000
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
    if source not in text or target not in text:
        return False
    return any(candidate in text for candidate in (
        f"{source} -> {target}",
        f"{target}\n  |<- {source}",
        f"{source}\n  |-> {target}",
    ))


def _finite_number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


@dataclass(frozen=True)
class SubwooferRuntimeConfig:
    """Normalized subwoofer settings used by measurement and AutoSub."""

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
    def from_overview(cls, overview: dict[str, Any]) -> "SubwooferRuntimeConfig":
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


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class DSPRuntimeConfig:
    output_mode: str
    output_key: str
    sample_rate: int
    hardware_ports: tuple[str, ...]
    layout: tuple[dict[str, Any], ...]

    @classmethod
    def from_overview(cls, overview: dict[str, Any]) -> "DSPRuntimeConfig":
        mode = overview.get("output_mode") or {}
        name = str(mode.get("mode") or "stereo")
        output = overview.get("selected_output") or overview.get("current_output") or {}
        output_key = str(mode.get("effective_output_key") or output.get("key") or output.get("name") or "")
        rate = int(mode.get("effective_output_rate") or output.get("active_rate") or overview.get("active_rate") or 48000)
        layout = [
            {"name": "FL", "routes": [{"input": 0, "gain": 1.0}]},
            {"name": "FR", "routes": [{"input": 1, "gain": 1.0}]},
        ]
        ports = ["playback_FL", "playback_FR"]
        if name.startswith("subwoofer-2."):
            ports.extend(("playback_RL", "playback_RR"))
            if name.startswith("subwoofer-2.2"):
                frequency = int(mode.get("crossover_frequency_hz") or 80)
                subs = mode.get("subwoofers") or {}
                sub1, sub2 = subs.get("sub1") or {}, subs.get("sub2") or {}
                align1, align2 = _number(sub1.get("alignment_ms")), _number(sub2.get("alignment_ms"))
                main_delay = max(0.0, -min(align1, align2))
                sub_defs = (sub1, sub2)
                stereo_bass = name == "subwoofer-2.2-stereo"
            else:
                sub = mode.get("subwoofer") or {}
                frequency = int(sub.get("crossover_frequency_hz") or 80)
                align1 = align2 = _number(sub.get("sub_alignment_ms"))
                main_delay = max(0.0, -align1)
                sub_defs = (sub, sub)
                stereo_bass = False
            highpass = bool((mode.get("subwoofer") or mode).get("main_highpass_enabled", True))
            if highpass:
                for channel in layout:
                    channel["filters"] = [{"type": "highpass", "frequency_hz": frequency, "q": 0.70710678, "stages": 2}]
            for channel in layout:
                channel["delay_ms"] = main_delay
            for index, definition in enumerate(sub_defs):
                alignment = align1 if index == 0 else align2
                routes = ([{"input": index, "gain": 1.0}] if stereo_bass else
                          [{"input": 0, "gain": 0.5}, {"input": 1, "gain": 0.5}])
                layout.append({
                    "name": f"SUB{index + 1}", "routes": routes,
                    "gain_db": _number(definition.get("level_db", definition.get("sub_level_db"))),
                    "delay_ms": main_delay + alignment if name.startswith("subwoofer-2.2") else max(0.0, alignment),
                    "invert": str(definition.get("polarity", definition.get("sub_polarity", "normal"))).lower() in {"invert", "inverted", "180"},
                    "filters": [{"type": "lowpass", "frequency_hz": frequency, "q": 0.70710678, "stages": 2}],
                })
        return cls(name, output_key, rate, tuple(ports), tuple(layout))


class DSPRuntime:
    def __init__(self, manager: Any, *, binary: str | Path | None = None,
                 command_runner: Callable[[Sequence[str]], Awaitable[CommandResult]] | None = None,
                 process_launcher: Callable[[Sequence[str]], Awaitable[Any]] | None = None):
        self.manager = manager
        self.binary = Path(binary or os.environ.get("FXROUTE_DSP_BINARY") or Path(__file__).parent / "native_dsp/build/fxroute-dsp")
        self._run = command_runner or self._run_command
        self._launch = process_launcher or self._launch_process
        self._process = None
        self._config: DSPRuntimeConfig | None = None
        self._config_path: Path | None = None
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

    @property
    def sync_in_progress(self) -> bool:
        return self._lock.locked()

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

    async def reclean_direct_easyeffects_links(self) -> None:
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
                "effect_bypass": self._effect_bypass, "output_gain_db": self._output_gain_db}

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
        data = await asyncio.wait_for(loop.sock_recv(self._control_socket, 4096), 1.0)
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
        async with self._measurement_scope_lock:
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
                self._started_at = time.time()
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
        expected.extend(f"{DSP_NODE_NAME}:output_{index + 1}" for index in range(len(config.hardware_ports)))
        expected.extend(f"{DSP_NODE_NAME}:{port}" for port in DSP_POST_EFFECT_PORTS)
        for _ in range(50):
            result = await self._run(("pw-link", "-io"))
            if result.returncode == 0 and all(port in result.stdout for port in expected):
                return
            if self._process is not None and getattr(self._process, "returncode", None) is not None:
                break
            await asyncio.sleep(.1)
        raise RuntimeError("Native DSP did not expose expected PipeWire ports")

    async def stop(self) -> None:
        for link in self._links:
            await self._run(("pw-link", "-d", link.source, link.target))
        self._links = []
        process, self._process = self._process, None
        if process is not None and getattr(process, "returncode", None) is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                process.kill(); await process.wait()
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

    async def verify(self) -> bool:
        if not self._links:
            return False
        result = await self._run(("pw-link", "-l"))
        return result.returncode == 0 and all(_contains_link(result.stdout, link.source, link.target) for link in self._links)

    @staticmethod
    async def _run_command(args: Sequence[str]) -> CommandResult:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        return CommandResult(process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace"))

    @staticmethod
    async def _launch_process(args: Sequence[str]) -> Any:
        return await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
