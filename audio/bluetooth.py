# SPDX-License-Identifier: AGPL-3.0-only
"""Bluetooth input monitoring, agent lifecycle and DSP link routing.

Owns the linked Bluetooth source name, the BlueZ agent subprocess and the
3-second monitor loop, moved out of ``main.py``.  No imports from ``main``:
the peak-monitor sync callback is injected by the composition root.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from audio import pw_link
from audio.samplerate import (
    SOURCE_MODE_BLUETOOTH_INPUT,
    disconnect_connected_bluetooth_audio_sources,
    get_audio_source_overview,
    get_bluetooth_audio_overview,
    set_bluetooth_receiver_enabled,
)

logger = logging.getLogger(__name__)

SourceModePeakSync = Callable[[dict[str, Any] | None], Awaitable[None]]


@dataclass
class BluetoothInputDependencies:
    """Live services the Bluetooth input monitor needs."""

    sync_peak_monitor_for_source_mode_state: SourceModePeakSync


class BluetoothInputMonitor:
    """Single owner of the Bluetooth input link, agent process and monitor loop."""

    def __init__(self, deps: BluetoothInputDependencies) -> None:
        self._deps = deps
        self.input_source_name: str | None = None
        self.agent_process: asyncio.subprocess.Process | None = None
        self.monitor_task: asyncio.Task | None = None

    async def _disconnect_source(self, source_name: str | None) -> None:
        normalized = (source_name or "").strip()
        if not normalized:
            return
        try:
            await self._link_source_to_dsp(normalized, disconnect=True)
        except Exception:
            pass

    async def stop_agent(self) -> None:
        proc = self.agent_process
        if not proc:
            return
        try:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        finally:
            if self.agent_process is proc:
                self.agent_process = None

    async def _ensure_agent(self) -> None:
        proc = self.agent_process
        if proc and proc.returncode is None:
            return
        agent_script = Path(__file__).resolve().parent / "bluez_agent.py"
        self.agent_process = await asyncio.create_subprocess_exec(
            str(agent_script),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.sleep(0.4)
        except BaseException:
            await self.stop_agent()
            raise
        if self.agent_process.returncode is not None:
            stderr = await self.agent_process.stderr.read()
            self.agent_process = None
            raise RuntimeError((stderr or b"BlueZ audio agent exited immediately").decode(errors="ignore").strip())

    async def clear_links(self) -> None:
        previous_source = self.input_source_name
        self.input_source_name = None
        await self._disconnect_source(previous_source)

    async def _link_source_to_dsp(self, source_name: str, disconnect: bool = False) -> None:
        normalized = (source_name or "").strip()
        if not normalized:
            return
        failures: list[str] = []
        for channel in ("FL", "FR"):
            sink_port = f"fxroute_dsp_sink:playback_{channel}"
            source_ports = (f"{normalized}:capture_{channel}", f"{normalized}:output_{channel}")
            try:
                if disconnect:
                    await pw_link.disconnect_ports(source_ports, sink_port)
                else:
                    await pw_link.connect_ports(source_ports, sink_port)
            except Exception as exc:
                failures.append(f"{channel}: {exc}")

        if failures:
            raise RuntimeError("failed to link ports: " + "; ".join(failures))

    async def disable(self) -> None:
        await self.clear_links()
        await self.stop_agent()
        try:
            disconnected = disconnect_connected_bluetooth_audio_sources()
            if disconnected:
                logger.info(
                    "Disconnected Bluetooth audio source devices while leaving bluetooth-input mode: %s",
                    ", ".join(disconnected),
                )
        except Exception as exc:
            logger.warning("Failed to disconnect Bluetooth audio source devices: %s", exc)

    async def _ensure_loopback(self, source_name: str) -> None:
        normalized = (source_name or "").strip()
        if not normalized:
            raise RuntimeError("Missing Bluetooth source name for monitoring")
        if self.input_source_name == normalized:
            return
        await self.clear_links()
        try:
            await self._link_source_to_dsp(normalized)
        except BaseException:
            await self._disconnect_source(normalized)
            raise
        self.input_source_name = normalized
        logger.info("Enabled Bluetooth input monitoring from %s to fxroute_dsp_sink", normalized)

    async def sync(self, source_overview: dict[str, Any] | None = None) -> dict[str, Any]:
        overview = source_overview or get_audio_source_overview()
        if overview.get("mode") != SOURCE_MODE_BLUETOOTH_INPUT:
            await self.disable()
            try:
                set_bluetooth_receiver_enabled(False)
            except Exception as exc:
                logger.warning("Failed to disable Bluetooth receiver mode: %s", exc)
            return overview

        bt_state = overview.get("bluetooth") or {}
        if not bt_state.get("selectable"):
            raise RuntimeError("Bluetooth input is not currently available")

        await self._ensure_agent()
        if not bt_state.get("discoverable") or not bt_state.get("pairable"):
            set_bluetooth_receiver_enabled(True)
        bt_overview = get_bluetooth_audio_overview()
        receiver_session = bt_overview.get("receiver_session") or {}
        source_name = receiver_session.get("source_name")
        if not source_name:
            await self.clear_links()
            return get_audio_source_overview()

        await self._ensure_loopback(str(source_name))
        return get_audio_source_overview()

    async def run_monitor_loop(self) -> None:
        while True:
            try:
                overview = get_audio_source_overview()
                if overview.get("mode") == SOURCE_MODE_BLUETOOTH_INPUT:
                    overview = await self.sync(overview)
                    await self._deps.sync_peak_monitor_for_source_mode_state(overview)
                elif self.input_source_name:
                    await self.disable()
                    await self._deps.sync_peak_monitor_for_source_mode_state(overview)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Bluetooth input monitor loop check failed: %s", exc)
            await asyncio.sleep(3)

    async def stop(self) -> None:
        if self.monitor_task is not None and not self.monitor_task.done():
            self.monitor_task.cancel()
        self.monitor_task = None
        await self.disable()
