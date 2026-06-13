"""PipeWire-native 2.1 Subwoofer Stage-3 runtime controller.

Stereo mode does not use this module; the existing EasyEffects/stereo path stays
unchanged. The old 2.1 subprocess bridge has been removed. This runtime owns
only the native helper lifecycle and graph links for Stage 3 crossover:
Out 1/2 = optional LR24 highpassed L/R, Out 3/4 = LR24 lowpassed
(L + R) * 0.5.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Awaitable, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

OUTPUT_MODE_SUBWOOFER_21 = "subwoofer-2.1"
DEFAULT_SAMPLE_RATE = 48_000
NATIVE_HELPER_PENDING_MESSAGE = "PipeWire-native 2.1 helper binary is not available"
NATIVE_HELPER_NODE_NAME = "fxroute_21_stage1"
NATIVE_HELPER_PORTS = ("input_L", "input_R", "output_1", "output_2", "output_3", "output_4")
EASYEFFECTS_SINK_NAME = "easyeffects_sink"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class PipeWireLink:
    source: str
    target: str


CommandRunner = Callable[[Sequence[str]], Awaitable[CommandResult]]
ProcessLauncher = Callable[[Sequence[str]], Awaitable[Any]]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class SubwooferRuntimeConfig:
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

    @property
    def derived_main_delay_ms(self) -> float:
        """Positive alignment delays sub, negative delays main. Returns ms to delay main speakers."""
        return max(0.0, -self.sub_alignment_ms)

    @property
    def derived_sub_delay_ms(self) -> float:
        """Positive alignment delays sub, negative delays main. Returns ms to delay subwoofer."""
        return max(0.0, self.sub_alignment_ms)

    @classmethod
    def from_overview(cls, overview: dict[str, Any]) -> "SubwooferRuntimeConfig":
        output_mode = overview.get("output_mode") or {}
        output = overview.get("selected_output") or overview.get("current_output") or {}
        subwoofer = output_mode.get("subwoofer") or {}
        output_key = str(output_mode.get("effective_output_key") or output.get("key") or output.get("name") or "").strip()
        output_label = str(output.get("label") or output.get("target_label") or output_key or "unknown output").strip()
        channels = _coerce_int(output_mode.get("effective_output_channels") or output.get("channels"), 0)
        sample_rate = _coerce_int(output_mode.get("effective_output_rate") or output.get("active_rate") or overview.get("active_rate"), DEFAULT_SAMPLE_RATE)
        if sample_rate <= 0:
            sample_rate = DEFAULT_SAMPLE_RATE
        return cls(
            output_mode=str(output_mode.get("mode") or "stereo"),
            output_key=output_key,
            output_label=output_label,
            output_channels=channels,
            sample_rate=sample_rate,
            crossover_frequency_hz=_clamp_int(subwoofer.get("crossover_frequency_hz"), 40, 200, 80),
            main_highpass_enabled=bool(subwoofer.get("main_highpass_enabled", True)),
            sub_level_db=_clamp_float(subwoofer.get("sub_level_db"), -24.0, 12.0, 0.0),
            sub_alignment_ms=_clamp_float_alignment(subwoofer),
            sub_polarity="invert" if str(subwoofer.get("sub_polarity") or "").lower() in {"invert", "inverted", "180"} else "normal",
        )


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(low, min(high, parsed))


def _clamp_float_alignment(subwoofer: dict[str, Any]) -> float:
    """Parse signed sub_alignment_ms from payload."""
    raw_alignment = subwoofer.get("sub_alignment_ms")
    try:
        parsed = float(raw_alignment)
    except (TypeError, ValueError):
        parsed = 0.0
    if not math.isfinite(parsed):
        parsed = 0.0
    return max(-40.0, min(40.0, round(parsed, 1)))


def _contains_link(text: str, source: str, target: str) -> bool:
    if source not in text or target not in text:
        return False
    direct = f"{source} -> {target}"
    reverse_pw_link_io = f"{target}\n  |<- {source}"
    forward_pw_link_io = f"{source}\n  |-> {target}"
    return direct in text or reverse_pw_link_io in text or forward_pw_link_io in text


class Subwoofer21Runtime:
    """Own the native 2.1 helper process and PipeWire graph links."""

    def __init__(
        self,
        *,
        helper_binary: str | Path | None = None,
        command_runner: CommandRunner | None = None,
        process_launcher: ProcessLauncher | None = None,
        sleeper: Sleeper | None = None,
        helper_node_name: str = NATIVE_HELPER_NODE_NAME,
        helper_quantum: int = 1024,
    ):
        self._config: Optional[SubwooferRuntimeConfig] = None
        self._last_error: Optional[str] = None
        self._helper_binary = Path(
            helper_binary
            or os.environ.get("FXROUTE_21_HELPER_BINARY")
            or Path(__file__).resolve().parent / "pipewire_stage1" / "build" / "fxroute_21_passthrough"
        )
        self._command_runner = command_runner or self._run_command
        self._process_launcher = process_launcher or self._launch_process
        self._sleep = sleeper or asyncio.sleep
        self._helper_node_name = helper_node_name
        self._helper_quantum = helper_quantum
        self._process: Any = None
        self._last_started_at: Optional[float] = None
        self._links_configured = False
        self._removed_direct_front_links = 0
        self._current_stream_key: Optional[tuple[Any, ...]] = None
        self._linked_output_key: Optional[str] = None
        self._needs_measurement_prime = False
        self._sync_lock = asyncio.Lock()
        self._pending_config: Optional[SubwooferRuntimeConfig] = None

    def _stream_key(self, config: SubwooferRuntimeConfig) -> tuple[Any, ...]:
        return (
            config.output_key,
            config.output_channels,
            config.sample_rate,
            config.crossover_frequency_hz,
            1 if config.main_highpass_enabled else 0,
            config.sub_level_db,
            config.sub_alignment_ms,
            config.sub_polarity,
        )

    def snapshot(self) -> dict[str, Any]:
        process_running = self._process is not None and getattr(self._process, "returncode", None) is None
        active = process_running and self._links_configured and self._last_error is None
        return {
            "active": active,
            "last_error": self._last_error,
            "last_started_at": self._last_started_at,
            "config": self._config.__dict__ if self._config else None,
            "stage": "stage4_sub_controls",
            "engine": "pipewire_native_helper",
            "implemented": True,
            "inactive_reason": self._last_error,
            "helper_binary": str(self._helper_binary),
            "helper_node": self._helper_node_name,
            "helper_pid": getattr(self._process, "pid", None) if process_running else None,
            "links_configured": self._links_configured,
            "removed_direct_front_links": self._removed_direct_front_links,
        }

    async def sync(self, config: SubwooferRuntimeConfig) -> None:
        self._pending_config = config
        if self._sync_lock.locked():
            logger.info(
                "2.1 runtime sync coalesced while reconfig is active: output_mode=%s crossover_hz=%s sample_rate=%s",
                config.output_mode,
                config.crossover_frequency_hz,
                config.sample_rate,
            )
            return

        async with self._sync_lock:
            while self._pending_config is not None:
                next_config = self._pending_config
                self._pending_config = None
                await self._sync_once(next_config)

    async def _sync_once(self, config: SubwooferRuntimeConfig) -> None:
        if config.output_mode != OUTPUT_MODE_SUBWOOFER_21:
            self._config = config
            await self.stop()
            await self._stop_orphan_helpers()
            self._last_error = None
            logger.info("2.1 runtime inactive: selected output mode is stereo")
            return
        if not config.output_key:
            self._config = config
            await self.stop()
            await self._stop_orphan_helpers()
            self._last_error = "2.1 Subwoofer requires a selected hardware output device"
            logger.warning("2.1 runtime inactive: %s", self._last_error)
            return
        elif config.output_channels < 4:
            self._config = config
            await self.stop()
            await self._stop_orphan_helpers()
            self._last_error = "2.1 Subwoofer requires a selected output device with at least 4 channels"
            logger.warning("2.1 runtime inactive: %s", self._last_error)
            return
        if not self._helper_binary.exists():
            self._config = config
            await self.stop()
            await self._stop_orphan_helpers()
            self._last_error = f"{NATIVE_HELPER_PENDING_MESSAGE}: {self._helper_binary}"
            logger.warning("2.1 runtime inactive: %s", self._last_error)
            return

        stream_key = self._stream_key(config)
        if self._process is None or getattr(self._process, "returncode", None) is not None or self._current_stream_key != stream_key:
            is_dsp_reconfig = (
                self._links_configured
                and self._config is not None
                and self._config.output_mode == OUTPUT_MODE_SUBWOOFER_21
            )
            if is_dsp_reconfig:
                self._needs_measurement_prime = True
                await self._stop_for_21_reconfig()
            else:
                await self.stop()
            await self._stop_orphan_helpers()
            try:
                await self._start_helper(config)
            except Exception as exc:
                logger.exception("Failed to start 2.1 native helper")
                await self._stop_for_21_reconfig()
                await self._stop_orphan_helpers()
                self._config = config
                self._current_stream_key = None
                self._last_error = str(exc)
                return
            needs_configure = True
        else:
            needs_configure = (
                not self._links_configured
                or self._linked_output_key != config.output_key
            )

        if needs_configure:
            try:
                await self._configure_graph(config)
                await self._verify_graph(config)
                self._config = config
                self._current_stream_key = stream_key
                self._last_error = None
            except Exception as exc:
                logger.exception("Failed to configure 2.1 native helper graph")
                await self._stop_for_21_reconfig()
                await self._stop_orphan_helpers()
                self._config = config
                self._current_stream_key = None
                self._last_error = str(exc)
                return
        else:
            self._config = config
            self._current_stream_key = stream_key
            self._last_error = None

        logger.info(
            "2.1 runtime active: output_mode=%s hardware_output=%s sample_rate=%s "
            "crossover_hz=%s main_highpass_enabled=%s "
            "fixed_routing='Out 1/2=optional LR24 highpassed L/R, Out 3/4=LR24 lowpassed (L+R)*0.5'",
            config.output_mode,
            config.output_key,
            config.sample_rate,
            config.crossover_frequency_hz,
            config.main_highpass_enabled,
        )

    @property
    def needs_measurement_prime(self) -> bool:
        return self._needs_measurement_prime

    @property
    def sync_in_progress(self) -> bool:
        return self._sync_lock.locked()

    def clear_measurement_prime(self) -> None:
        self._needs_measurement_prime = False

    async def _stop_helper_and_links_only(self) -> None:
        """Remove helper graph links and stop the helper process.

        Does NOT restore direct EasyEffects -> hardware front links.
        Used for in-place 2.1 DSP reconfiguration where the output
        mode stays subwoofer-2.1 and only parameters change.
        Restoring direct front links during a reconfig can create a
        stale PipeWire graph transient that interferes with the next
        measurement capture path.
        """
        await self._remove_graph_links()
        await self._stop_helper()
        self._links_configured = False
        self._linked_output_key = None
        self._removed_direct_front_links = 0

    async def _stop_for_21_reconfig(self) -> None:
        """Stop helper and links only; keep 2.1 output mode context.

        Does not clear self._last_error or self._current_stream_key
        so the caller owns the transition state.
        """
        await self._stop_helper_and_links_only()

    async def stop(self) -> None:
        """Full stop for Stereo mode switch or shutdown.

        Removes helper links, stops the helper, and restores
        direct EasyEffects -> hardware front links so stereo
        playback works through the normal signal chain.
        """
        output_key = self._linked_output_key or (self._config.output_key if self._config else "")
        await self._remove_graph_links()
        await self._stop_helper()
        if output_key:
            try:
                await self._restore_direct_easyeffects_front_links(output_key)
            except Exception as exc:
                logger.warning("Failed to restore Stereo EasyEffects front links during 2.1 stop (output_key=%s): %s", output_key, exc)
        self._last_error = None
        self._links_configured = False
        self._current_stream_key = None
        self._removed_direct_front_links = 0

    def _stage1_links(self, output_key: str) -> list[PipeWireLink]:
        return [
            PipeWireLink("ee_soe_output_level:output_FL", f"{self._helper_node_name}:input_L"),
            PipeWireLink("ee_soe_output_level:output_FR", f"{self._helper_node_name}:input_R"),
            PipeWireLink(f"{self._helper_node_name}:output_1", f"{output_key}:playback_FL"),
            PipeWireLink(f"{self._helper_node_name}:output_2", f"{output_key}:playback_FR"),
            PipeWireLink(f"{self._helper_node_name}:output_3", f"{output_key}:playback_RL"),
            PipeWireLink(f"{self._helper_node_name}:output_4", f"{output_key}:playback_RR"),
        ]

    def _direct_easyeffects_front_links(self, output_key: str) -> list[PipeWireLink]:
        return [
            PipeWireLink("ee_soe_output_level:output_FL", f"{output_key}:playback_FL"),
            PipeWireLink("ee_soe_output_level:output_FR", f"{output_key}:playback_FR"),
        ]

    async def _start_helper(self, config: SubwooferRuntimeConfig) -> None:
        args = [
            str(self._helper_binary),
            "--node-name",
            self._helper_node_name,
            "--rate",
            str(config.sample_rate),
            "--quantum",
            str(self._helper_quantum),
            "--lowpass-hz",
            str(config.crossover_frequency_hz),
            "--highpass-hz",
            str(config.crossover_frequency_hz if config.main_highpass_enabled else 0),
            "--sub-level-db",
            str(config.sub_level_db),
            "--sub-polarity",
            config.sub_polarity,
            "--main-delay-ms",
            str(config.derived_main_delay_ms),
            "--sub-delay-ms",
            str(config.derived_sub_delay_ms),
        ]
        logger.info("Starting 2.1 helper: %s", shlex.join(args))
        self._process = await self._process_launcher(args)
        self._last_started_at = time.time()
        await self._wait_for_helper_ports()

    async def _configure_graph(self, config: SubwooferRuntimeConfig) -> None:
        self._removed_direct_front_links = 0
        self._linked_output_key = config.output_key
        await self._remove_direct_easyeffects_front_links(config.output_key)
        for link in self._stage1_links(config.output_key):
            await self._unlink(link, ignore_errors=True)
            await self._link(link)
        # EasyEffects can reconnect its front outputs when the sink becomes active.
        # Remove them once more after the helper graph exists; steady-state event
        # repair belongs to a later graph-watch pass, not a polling loop here.
        await self._remove_direct_easyeffects_front_links(config.output_key)
        self._links_configured = True

    async def _verify_graph(self, config: SubwooferRuntimeConfig) -> None:
        result = await self._command_runner(["pw-link", "-l"])
        if result.returncode != 0:
            raise RuntimeError(f"pw-link -l failed while verifying 2.1 graph: {result.stderr or result.stdout}".strip())
        text = result.stdout or ""
        missing = [link for link in self._stage1_links(config.output_key) if not _contains_link(text, link.source, link.target)]
        if missing:
            formatted = ", ".join(f"{link.source} -> {link.target}" for link in missing)
            raise RuntimeError(f"2.1 helper graph verification failed; missing links: {formatted}")
        direct = [link for link in self._direct_easyeffects_front_links(config.output_key) if _contains_link(text, link.source, link.target)]
        if direct:
            formatted = ", ".join(f"{link.source} -> {link.target}" for link in direct)
            raise RuntimeError(f"2.1 helper graph verification failed; direct EasyEffects links remain: {formatted}")

    async def _remove_graph_links(self) -> None:
        output_key = self._linked_output_key or (self._config.output_key if self._config else "")
        if not output_key:
            return
        for link in self._stage1_links(output_key):
            await self._unlink(link, ignore_errors=True)
        self._links_configured = False
        self._linked_output_key = None

    async def _remove_direct_easyeffects_front_links(self, output_key: str) -> None:
        for link in self._direct_easyeffects_front_links(output_key):
            result = await self._unlink(link, ignore_errors=True)
            if result.returncode == 0:
                self._removed_direct_front_links += 1

    async def _restore_direct_easyeffects_front_links(self, output_key: str) -> None:
        for link in self._direct_easyeffects_front_links(output_key):
            await self._unlink(link, ignore_errors=True)
            await self._link(link)

    async def reclean_direct_easyeffects_links(self) -> None:
        """Remove any direct EE front links that EasyEffects may have re-created
        after a preset load while the 2.1 helper graph owns the output.

        Removes direct ee_soe_output_level -> hardware playback links and
        re-verifies the helper input links from ee_soe_output_level ports.
        Does not touch helper output links or the helper process.
        """
        if not self._links_configured or not self._linked_output_key:
            return
        await self._remove_direct_easyeffects_front_links(self._linked_output_key)
        # Re-verify helper input links; EasyEffects preset reload may disconnect them
        for channel, helper_input in [("FL", "input_L"), ("FR", "input_R")]:
            link = PipeWireLink(f"ee_soe_output_level:output_{channel}", f"{self._helper_node_name}:{helper_input}")
            await self._unlink(link, ignore_errors=True)
            await self._link(link)

    async def _wait_for_helper_ports(self) -> None:
        expected = [f"{self._helper_node_name}:{port}" for port in NATIVE_HELPER_PORTS]
        for _ in range(40):
            if self._process is not None and getattr(self._process, "returncode", None) is not None:
                raise RuntimeError(f"2.1 helper exited before exposing ports: returncode={self._process.returncode}")
            result = await self._command_runner(["pw-link", "-io"])
            if result.returncode == 0 and all(port in result.stdout for port in expected):
                return
            await self._sleep(0.1)
        raise RuntimeError(f"2.1 helper did not expose expected ports: {', '.join(expected)}")

    async def _link(self, link: PipeWireLink) -> CommandResult:
        result = await self._command_runner(["pw-link", link.source, link.target])
        if result.returncode != 0:
            message = f"{result.stderr or result.stdout}".strip()
            if "File exists" in message:
                logger.info("2.1 runtime link already exists: %s -> %s", link.source, link.target)
                return result
            raise RuntimeError(f"pw-link failed: {link.source} -> {link.target}: {result.stderr or result.stdout}".strip())
        return result

    async def _unlink(self, link: PipeWireLink, *, ignore_errors: bool = False) -> CommandResult:
        result = await self._command_runner(["pw-link", "-d", link.source, link.target])
        if result.returncode != 0 and not ignore_errors:
            raise RuntimeError(f"pw-link -d failed: {link.source} -> {link.target}: {result.stderr or result.stdout}".strip())
        return result

    async def _stop_helper(self) -> None:
        process = self._process
        self._process = None
        if process is None or getattr(process, "returncode", None) is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def _stop_orphan_helpers(self) -> None:
        pattern = f"{self._helper_binary}.*--node-name {self._helper_node_name}"
        result = await self._command_runner(["pgrep", "-f", pattern])
        orphan_pids = result.stdout.strip()
        if not orphan_pids:
            return
        logger.info("Found orphan 2.1 helpers (pids: %s), cleaning up", orphan_pids.replace("\n", ", "))
        await self._command_runner(["pkill", "-TERM", "-f", pattern])
        await self._sleep(0.3)

    @staticmethod
    async def _run_command(args: Sequence[str]) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return CommandResult(
            process.returncode,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    @staticmethod
    async def _launch_process(args: Sequence[str]) -> Any:
        return await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
