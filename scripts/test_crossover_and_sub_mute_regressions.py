#!/usr/bin/env python3
"""Regression tests: crossover persistence (2.2 readback) and same-mode mute.

Bug 1: A 2.2 crossover edit was persisted to the top-level 2.2 payload, but
`_load_audio_output_mode()` re-derived the global fields from the stale legacy
2.1 `subwoofer` block (or the 80 Hz default when no legacy block existed), so
every readback forced the UI back to 80 Hz.

Bug 2: Every topology edit went through the Coordinator's muted
`output-mode-switch` transition, closing the hardware-output gate even
for pure DSP parameter changes (level, alignment, polarity, crossover) that
change no routing, samplerate or graph topology. Non-topology v2 mutations
must commit directly without the Coordinator instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playback.transition import PlaybackTransitionCoordinator, TransitionRequest

import audio.samplerate as samplerate
import main
import playback.orchestration as playback_orchestration
import dsp.api as dsp_api

dsp_api.configure_dsp_api(main._make_dsp_api_deps())


# NOTE (backend-v2 migration): the legacy mode-file round-trip (Bug 1) is
# deleted with the persistence helpers. Crossover translation from the v2
# head is covered by test_derived_output_mode.py (file parity test).


class FakeRequest:
    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


def _v2_seed(service):
    from audio.output_state import set_mode_routing, switch_mode
    return service.apply(
        lambda state: switch_mode(set_mode_routing(
            state, "stereo-sub", "A", ["main_l", "main_r", "sub1", "sub1"]), "stereo-sub"),
        expected_revision=0)


def _v2_context(service, manager, run):
    """Route harness for the v2 apply endpoint (mirrors the coordinator suite)."""
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.multiple(
        main,
        measurement_sr_session=mock.MagicMock(has_active_jobs=False),
        get_output_service=mock.MagicMock(return_value=service),
        get_audio_output_overview=mock.MagicMock(return_value={
            "selected_output": {"key": "A", "channels": 4},
            "output_mode": {"effective_output_key": "A", "effective_output_channels": 4,
                            "hardware_playback_ports": [f"playback_AUX{i}" for i in range(4)]},
        }),
        get_samplerate_status=mock.MagicMock(return_value={"active_rate": 48000}),
        _require_dsp_manager=mock.MagicMock(return_value=manager),
        _coordinator_current_playback_context=mock.AsyncMock(return_value={
            "source": "local", "target_url": None, "target_track": {}, "should_play": False,
        }),
        _run_coordinated_transition=run,
    ))
    stack.enter_context(mock.patch.object(main.runtime, "dsp_runtime", None))
    return stack


def _v2_service(raw: str):
    from audio.output_service import OutputService, OutputServiceDeps
    from audio.output_state_store import OutputStateStore
    from dsp.manager import DSPManager
    manager = DSPManager(home=Path(raw) / "home")
    service = OutputService(OutputServiceDeps(
        store=OutputStateStore(Path(raw) / "output-state.json"),
        preset_loader=manager.preset_store.read,
        resolve_ir=lambda kernel: (_ for _ in ()).throw(AssertionError(kernel)),
        measurement_active=lambda: False))
    _v2_seed(service)
    return service, manager


async def _route_same_mode_direct() -> None:
    """Bug 2 (v2): a non-topology param edit commits directly, no coordinator."""
    with tempfile.TemporaryDirectory() as raw:
        service, manager = _v2_service(raw)
        run = mock.AsyncMock()
        with _v2_context(service, manager, run):
            result = await main.apply_audio_output_state(FakeRequest({
                "expected_revision": 1,
                "mutation": {"kind": "set_crossover", "mode": "stereo-sub",
                             "enabled": False},
            }))
        run.assert_not_awaited()
        assert result["live_applied"] is False
        assert service.load()["revision"] == 2
    print("same-param edit commits directly without coordinator: ok")


async def _route_mode_switch_coordinated() -> None:
    """A real topology switch still needs the muted Coordinator transition."""
    with tempfile.TemporaryDirectory() as raw:
        service, manager = _v2_service(raw)
        run = mock.AsyncMock()
        with _v2_context(service, manager, run):
            result = await main.apply_audio_output_state(FakeRequest({
                "expected_revision": 1,
                "mutation": {"kind": "set_routing", "mode": "stereo-sub",
                             "assignments": ["main_l", "main_r", "sub1", "sub2"]},
            }))
        run.assert_awaited_once()
        request = run.await_args.args[0]
        assert request.operation == "output-mode-switch"
        assert request.output_state_transition["expected_revision"] == 1
        assert "candidate_state" in request.output_state_transition
        assert result["live_applied"] is True
    print("topology switch keeps coordinated transition: ok")




RADIO_URL = "https://ice4.somafm.com/groovesalad-256-mp3"


def _radio_recovery_request(detail: str = "subwoofer-link-watcher") -> TransitionRequest:
    return TransitionRequest(
        operation="recovery",
        source="radio",
        detail=detail,
        target_url=RADIO_URL,
        should_play=True,
        recovery_commit_context_id="tr-123",
        recovery_source="radio",
        recovery_url=RADIO_URL,
    )


def _fake_coordinator(*, current: bool, latched: bool = False, commit: str = "tr-123", active: bool = False):
    return mock.MagicMock(
        recovery_context_is_current=mock.MagicMock(return_value=current),
        last_successful_commit_id=commit,
        transition_active=active,
        gate=mock.MagicMock(failure_latched=latched),
    )


def _idle_runtime() -> mock.MagicMock:
    return mock.MagicMock(sync_in_progress=False)


async def _recovery_valid(
    request: TransitionRequest,
    *,
    coordinator: mock.MagicMock,
    runtime: mock.MagicMock | None,
) -> bool:
    with mock.patch.object(main, "playback_transition_coordinator", coordinator), \
            mock.patch.object(main.runtime, "dsp_runtime", runtime), \
            mock.patch.object(
                main.runtime, "player_instance",
                mock.MagicMock(state={"current_file": RADIO_URL, "ended": False}),
            ), \
            mock.patch.object(
                main.playback_state, "current_track_info",
                {"source": "radio", "url": RADIO_URL},
            ):
        return await playback_orchestration.configured().recovery_context_is_valid(request)


async def _link_watcher_latch_reentry() -> None:
    """The subwoofer link watcher may re-enter across a latched gate."""
    latched = _fake_coordinator(current=False, latched=True)
    assert await _recovery_valid(_radio_recovery_request(), coordinator=latched, runtime=_idle_runtime()) is True
    print("link watcher recovery re-enters a latched gate: ok")


async def _link_watcher_kept_out_for_other_reasons() -> None:
    """Non-watcher recoveries, stale commits and active transitions stay out."""
    other = _radio_recovery_request(detail="samplerate-drift-watcher")
    assert await _recovery_valid(other, coordinator=_fake_coordinator(current=False, latched=True), runtime=_idle_runtime()) is False
    stale = _fake_coordinator(current=False, latched=True, commit="tr-999")
    assert await _recovery_valid(_radio_recovery_request(), coordinator=stale, runtime=_idle_runtime()) is False
    active = _fake_coordinator(current=False, latched=True, active=True)
    assert await _recovery_valid(_radio_recovery_request(), coordinator=active, runtime=_idle_runtime()) is False
    print("latched-gate re-entry stays limited to the link watcher: ok")


async def _recovery_deferred_during_subwoofer_sync() -> None:
    """No watcher recovery may start while the runtime reconfigures links."""
    running = mock.MagicMock(sync_in_progress=True)
    assert await _recovery_valid(_radio_recovery_request(), coordinator=_fake_coordinator(current=True), runtime=running) is False
    assert await _recovery_valid(_radio_recovery_request(), coordinator=_fake_coordinator(current=True), runtime=None) is True
    print("recovery deferred while subwoofer runtime reconfigures: ok")


async def _runtime_sync_in_progress_flag() -> None:
    """DSPRuntime.sync_in_progress tracks the active reconfig lock."""
    runtime = main.DSPRuntime(mock.MagicMock())
    assert runtime.sync_in_progress is False
    lock = asyncio.Lock()
    runtime._lock = lock
    async with lock:
        assert runtime.sync_in_progress is True
    assert runtime.sync_in_progress is False
    print("DSPRuntime.sync_in_progress flag: ok")





class VolumeSwitchRuntime:
    """Fake runtime for the mode-switch volume regression.

    The audible volume (``volume``) mirrors the canonical user volume
    (``canonical_volume``) unless a mode switch resurrects a stale preset
    work point (mimicking the native DSP graph rebuild re-applying its own
    preset loudness state).  The Coordinator's DSP
    stabilization must repair that by re-applying the canonical volume.
    """

    def __init__(self, *, canonical_volume: int, stale_volume: int):
        self.canonical_volume = canonical_volume
        self.volume = canonical_volume
        self.stale_volume = stale_volume
        self.muted = False
        self.dsp_muted = False
        self.events: list[str] = []
        self.direct_bypass = False

    def set_volume(self, volume: int) -> None:
        self.canonical_volume = volume
        self.volume = volume

    def resurrect_stale_volume(self) -> None:
        self.volume = self.stale_volume

    async def read_hardware_mute(self) -> bool:
        return self.muted

    async def set_hardware_mute(self, muted: bool, _transition_id: str) -> None:
        self.muted = bool(muted)
        if not muted:
            self.direct_bypass = True

    async def read_sink_mute(self, _sink_name: str) -> bool:
        return self.dsp_muted

    async def set_sink_mute(self, _sink_name: str, muted: bool, _transition_id: str) -> None:
        self.dsp_muted = bool(muted)

    async def read_transition_snapshot(self, _request) -> dict:
        return {
            "player": {
                "current_file": "/music/current.flac",
                "playing": True,
                "paused": False,
                "volume": 100,
            },
            "output_mode_overview": {"output_mode": {"mode": "stereo"}},
            "output_mode_config": {"mode": "stereo"},
            "spotify": {"status": "Playing"},
        }

    async def quiet_old_source(self, _request) -> None:
        self.events.append("quiet")

    async def resolve_target_rate(self, request) -> int:
        return request.target_rate

    async def establish_target_rate(self, request) -> None:
        self.events.append("target-rate")

    async def establish_effects_and_helper(self, _request) -> dict:
        self.events.append("effects-helper-links")
        self.resurrect_stale_volume()
        return {"dsp_reinitialized": False}

    async def restore_output_mode_transport(self, _request, _snapshot, _transition_id) -> None:
        self.events.append("restore-transport")

    async def reconcile_post_start_graph(self, _request) -> dict:
        return {"graph_complete": True}

    async def verify_output_mode_runtime(self, _request) -> dict:
        return {"committed": True, "graph_complete": True}

    async def commit_output_mode_runtime(self, _request) -> dict:
        self.events.append("persist")
        return {"output_mode_persisted": True}

    async def finalize_output_mode_graph_after_gate_open(self, _request) -> dict:
        self.events.append("post-gate-graph")
        assert not self.muted, "final graph cleanup must run after gate open"
        assert self.direct_bypass, "test must reproduce gate-open direct bypass"
        self.direct_bypass = False
        return {"graph_complete": True}

    async def stabilize_effects_after_rate_change(
        self, _request, *, dsp_reinitialized: bool = False
    ) -> dict:
        self.events.append("dsp-stabilize")
        self.volume = self.canonical_volume
        return {"stabilized": True}

    async def pause_source_after_failure(self, _request) -> None:
        self.events.append("pause-after-failure")

    async def abort_failed_transition(self, _request, _snapshot, *, target_staged) -> None:
        self.events.append(f"abort:{target_staged}")

    def target_source_staged(self, _request) -> bool:
        return False

    async def verify_transition_graph(self, _request) -> dict:
        return {"committed": True}

    async def verify_committed_transition(self, _request) -> dict:
        return {"committed": True}

    async def prepare_target_source(self, _request) -> None:
        pass

    async def start_target_source(self, _request) -> None:
        pass

    async def set_source_volume(self, _volume: int, _transition_id: str) -> None:
        pass


def _mode_request(mode: str, *, operation: str = "output-mode-switch") -> TransitionRequest:
    return TransitionRequest(
        operation=operation,
        source="local",
        target_rate=44100,
        target_url="/music/current.flac",
        target_track={"source": "local", "url": "/music/current.flac"},
        should_play=True,
        rate_change=False,
        reload_source=False,
        output_mode_target={"output_mode": {"mode": mode}},
    )


async def _mode_switch_volume_preserved() -> None:
    """Volume X in A -> B -> volume Y -> back to A: Y must survive.

    The DSP preset reload / graph rebuild re-applies a stale preset
    loudness work point over the canonical user volume; the output-mode
    switch must always re-apply the canonical volume instead of resurrecting
    the older value.
    """
    runtime = VolumeSwitchRuntime(canonical_volume=40, stale_volume=20)
    coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

    # Mode A (stereo) at volume X=40.
    assert runtime.volume == 40
    # A -> B (2.2): stale preset volume resurrects, switch must re-apply 40.
    result = await coordinator.execute(_mode_request("subwoofer-2.2"))
    assert result.committed, result
    assert runtime.volume == 40, runtime.volume
    assert "dsp-stabilize" in runtime.events
    assert not runtime.direct_bypass
    assert runtime.events[-1] == "post-gate-graph"

    # Volume changed to Y=75 while in mode B.
    runtime.set_volume(75)
    assert runtime.volume == 75

    # B -> A (stereo): the old mode-A value (40) must NOT come back.
    result = await coordinator.execute(_mode_request("stereo"))
    assert result.committed, result
    assert runtime.volume == 75, runtime.volume
    assert not runtime.direct_bypass

    # Reverse direction: A -> B keeps Y as well.
    result = await coordinator.execute(_mode_request("subwoofer-2.2"))
    assert result.committed, result
    assert runtime.volume == 75, runtime.volume
    assert runtime.events.count("dsp-stabilize") == 3, runtime.events
    assert runtime.events.count("post-gate-graph") == 3, runtime.events

    # Control: a plain play transition without DSP reinit keeps the old
    # no-stabilize behavior (the fix must not widen the gate).
    control = VolumeSwitchRuntime(canonical_volume=50, stale_volume=20)
    control_coordinator = PlaybackTransitionCoordinator(control, gate_settle_seconds=0)
    result = await control_coordinator.execute(
        _mode_request("stereo", operation="play")
    )
    assert result.committed, result
    assert "dsp-stabilize" not in control.events, control.events
    print("mode-switch volume survives preset resurrection (both directions): ok")





async def _preset_load_reclean_skipped_during_sync() -> None:
    """A/B flip repair must not race an in-flight subwoofer sync."""
    active_runtime = mock.MagicMock(
        snapshot=mock.MagicMock(return_value={"active": True}),
        sync_in_progress=True,
        sync=mock.AsyncMock(),
        _reclean_guarded=mock.AsyncMock(),
    )
    dsp_manager = mock.MagicMock()
    dsp_manager.load_preset.return_value = None
    dsp_manager.load_compare_state.return_value = {"presetA": "Neutral", "presetB": "B", "activeSide": None}
    dsp_manager.get_status.return_value = {"active_preset": "Neutral", "compare": {}}
    broadcast = mock.AsyncMock()
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.multiple(
        main,
        _require_dsp_manager=mock.MagicMock(return_value=dsp_manager),
        manager=mock.MagicMock(broadcast=broadcast),
    ))
    stack.enter_context(mock.patch.multiple(main.runtime, dsp_runtime=active_runtime))
    stack.enter_context(mock.patch.object(
        main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change", mock.MagicMock()
    ))
    with stack:
        await dsp_api.load_dsp_preset(FakeRequest({"preset_name": "Neutral"}))
    active_runtime._reclean_guarded.assert_not_awaited()

    idle_runtime = mock.MagicMock(
        snapshot=mock.MagicMock(return_value={"active": True}),
        sync_in_progress=False,
        sync=mock.AsyncMock(),
        _reclean_guarded=mock.AsyncMock(),
    )
    stack2 = contextlib.ExitStack()
    stack2.enter_context(mock.patch.multiple(
        main,
        _require_dsp_manager=mock.MagicMock(return_value=dsp_manager),
        manager=mock.MagicMock(broadcast=mock.AsyncMock()),
    ))
    stack2.enter_context(mock.patch.multiple(main.runtime, dsp_runtime=idle_runtime))
    stack2.enter_context(mock.patch.object(
        main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change", mock.MagicMock()
    ))
    with stack2:
        await dsp_api.load_dsp_preset(FakeRequest({"preset_name": "Neutral"}))
    idle_runtime._reclean_guarded.assert_awaited_once()
    print("preset-load reclean defers to an in-flight subwoofer sync: ok")


async def _mode_switch_reapplies_compare_after_runtime_sync() -> None:
    """Stereo recovery restores the latest A/B side after a DSP restart."""
    active_preset = "A"
    events = []
    dsp_manager = mock.MagicMock()
    dsp_manager.load_compare_state.side_effect = [
        {"presetA": "A", "presetB": "B", "activeSide": "A"},
        # Model a user selecting B while runtime sync restarts the DSP.
        {"presetA": "A", "presetB": "B", "activeSide": "B"},
    ]
    dsp_manager.get_active_preset.side_effect = lambda: active_preset

    def load_preset(name, *, convolver_sample_rate_hz=None):
        nonlocal active_preset
        events.append(("load", name, convolver_sample_rate_hz))
        active_preset = name

    dsp_manager.load_preset.side_effect = load_preset

    async def sync_runtime(**_kwargs):
        nonlocal active_preset
        events.append(("runtime-sync",))
        if len(events) == 1:
            active_preset = "stale-dsp-restart-preset"

    wait_for_ports = mock.AsyncMock(return_value=True)
    complete_graph = {
        "dsp_ports": True,
        "links_complete": True,
        "links": {},
        "signature": "stereo-complete",
    }
    request = replace(
        _mode_request("stereo"),
        output_mode_target={
            "output_mode": {
                "mode": "stereo",
                "effective_output_key": "mock_output",
            }
        },
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.multiple(
            main,
            dsp_manager=dsp_manager,
        ))
        stack.enter_context(mock.patch.object(
            playback_orchestration.configured(), "playback_graph_diagnosis",
            mock.AsyncMock(return_value=complete_graph),
        ))
        stack.enter_context(mock.patch.object(
            playback_orchestration.configured(), "wait_for_dsp_output_ports", wait_for_ports
        ))
        stack.enter_context(mock.patch.object(
            playback_orchestration.configured(), "repair_stereo_output_links_once", mock.AsyncMock()
        ))
        stack.enter_context(mock.patch.object(
            samplerate, "reconcile_transition_sink_rate", mock.AsyncMock(return_value=True)
        ))
        stack.enter_context(mock.patch.multiple(main.runtime, dsp_preset_load_lock=asyncio.Lock()))
        stack.enter_context(mock.patch.object(
            main.dsp_orchestrator, "sync_runtime", mock.AsyncMock(side_effect=sync_runtime)
        ))
        result = await playback_orchestration.configured().establish_effects_and_helper(request)

    assert result["preset_reloaded"] is True
    assert events == [
        ("runtime-sync",),
        ("load", "B", 44100),
        ("runtime-sync",),
    ], events
    assert active_preset == "B"
    assert wait_for_ports.await_count == 2
    print("mode switch restores latest compare preset after runtime sync: ok")


async def _runtime_sync_in_progress_covers_link_repair() -> None:
    """sync_in_progress must cover the preset-load link repair lock."""
    runtime = main.DSPRuntime(mock.MagicMock())
    lock = asyncio.Lock()
    runtime._lock = lock
    async with lock:
        assert runtime.sync_in_progress is True
    assert runtime.sync_in_progress is False
    print("DSPRuntime.sync_in_progress covers link repair: ok")


async def main_async() -> None:
    await _route_same_mode_direct()
    await _route_mode_switch_coordinated()
    await _link_watcher_latch_reentry()
    await _link_watcher_kept_out_for_other_reasons()
    await _recovery_deferred_during_subwoofer_sync()
    await _runtime_sync_in_progress_flag()
    await _mode_switch_volume_preserved()
    await _preset_load_reclean_skipped_during_sync()
    await _mode_switch_reapplies_compare_after_runtime_sync()
    await _runtime_sync_in_progress_covers_link_repair()
    print("crossover / sub mute regression tests: ok")


if __name__ == "__main__":
    asyncio.run(main_async())
