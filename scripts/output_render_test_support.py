# SPDX-License-Identifier: AGPL-3.0-only
"""Main-free fixtures for output rendering and recovery contracts."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state_store import OutputStateStore
from dsp.manager import DSPManager, ensure_kernel_supported_ir, parse_wav_frames
from dsp.output_render import OutputRenderer, OutputRenderDeps

PORTS = [f"playback_AUX{i}" for i in range(4)]


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.pid = 123
        self.stderr = None

    def terminate(self):
        self.returncode = 0

    async def wait(self):
        return self.returncode


def make_manager(directory):
    manager = DSPManager(home=Path(directory) / "home")
    for bank in ("sub1", "global"):
        manager.library(bank, "stereo-sub").write(
            "Room", {"schema": "fxroute.dsp.preset", "version": 1, "chain": []})
    manager.save_global_extras({"limiter": {"enabled": False}})
    return manager


def resolve_ir(manager, bank_id, kernel):
    path = manager._resolve_kernel_path(kernel, manager.library(bank_id))
    params = parse_wav_frames(path)
    ensure_kernel_supported_ir(params, path.name)
    return {"path": str(path), "channels": params["channels"]}


def make_service(directory, manager):
    return OutputService(OutputServiceDeps(
        store=OutputStateStore(Path(directory) / "output-state.json"),
        preset_loader=manager.read_preset,
        resolve_ir=lambda _bank, kernel: (_ for _ in ()).throw(AssertionError(kernel)),
        measurement_active=lambda: False,
        global_extras_loader=manager.load_global_extras))


def pw_link_io(*ports):
    return "\n".join(f"fxroute_dsp:{port}" for port in ports)


class RenderHarness:
    def __init__(self, service, manager, native):
        self.service, self.manager, self.native = service, manager, native
        self.overview = {
            "selected_output": {"key": "A", "channels": 4},
            "output_mode": {"effective_output_key": "A", "effective_output_channels": 4,
                            "hardware_playback_ports": PORTS}}
        self.graph_lock = asyncio.Lock()
        self.mutation_lock = asyncio.Lock()
        self.renderer = OutputRenderer(OutputRenderDeps(
            get_service=lambda: self.service, get_manager=lambda: self.manager,
            get_runtime=lambda: self.native, get_overview=lambda: self.overview,
            get_status=lambda: {"active_rate": 48000}, graph_write_lock=lambda: self.graph_lock,
            mutation_lock=lambda: self.mutation_lock,
            volume_state_for_manager=AsyncMock(return_value=SimpleNamespace(
                loudness_in_path=False, preset="Neutral", dsp_guard_db=0.0, master_percent=27))))
