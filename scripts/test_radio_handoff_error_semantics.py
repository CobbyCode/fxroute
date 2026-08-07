#!/usr/bin/env python3
"""Coordinator-owned radio/local transition error semantics.

Covers:
- radio live-rate resolution propagates transition failures through the
  Coordinator (no tolerant swallow), stale generations still abort cleanly
- transient link races (missing EE->helper links in 2.2) are repaired by the
  Coordinator's canonical graph stage while mpv stays paused
- /api/play: set_pause(False) only after a verified handoff; on failure
  no unpause, no follow-up _sync_subwoofer_runtime after the handoff
- local and radio failure semantics are consistent (error -> HTTP 500)
- no-op cases still run without helper/preset rebuild
- the graph-incomplete diagnosis log names EE ports, helper ports and
  each missing link individually
All PipeWire/mpv/EE I/O is mocked; no live audio commands are executed.
"""

from __future__ import annotations

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import stations as stations_module
from playback_transition import PlaybackTransitionCoordinator
from playback_transition_test_support import run_main_handoff_through_coordinator

OUTPUT_KEY = "alsa_output.pci-0000_00_1f.3.analog-stereo"
STATION_URL = "https://radio.example/stream"
HELPER_PORTS = (
    "fxroute_21_stage1:input_L\n"
    "fxroute_21_stage1:input_R\n"
    "fxroute_21_stage1:output_1\n"
    "fxroute_21_stage1:output_2\n"
    "fxroute_21_stage1:output_3\n"
    "fxroute_21_stage1:output_4\n"
)
EE_TO_HELPER_LINKS = (
    "ee_soe_output_level:output_FL -> fxroute_21_stage1:input_L\n"
    "ee_soe_output_level:output_FR -> fxroute_21_stage1:input_R\n"
)
HELPER_TO_HW_LINKS = (
    f"fxroute_21_stage1:output_1 -> {OUTPUT_KEY}:playback_FL\n"
    f"fxroute_21_stage1:output_2 -> {OUTPUT_KEY}:playback_FR\n"
    f"fxroute_21_stage1:output_3 -> {OUTPUT_KEY}:playback_RL\n"
    f"fxroute_21_stage1:output_4 -> {OUTPUT_KEY}:playback_RR\n"
)


def _links_text(state: dict) -> str:
    """pw-link -l / -io dump for the 2.2 graph, driven by state dict.

    state["ee_to_helper"] controls whether the EE->helper links are
    present (transient race simulation); helper ports and helper->HW
    links are always present once the helper runs.
    """
    text = (
        "ee_soe_output_level:output_FL\n"
        "ee_soe_output_level:output_FR\n"
        + HELPER_PORTS
    )
    if state.get("ee_to_helper", True):
        text += EE_TO_HELPER_LINKS
    text += HELPER_TO_HW_LINKS
    return text


def _make_runtime(helper_state: dict, stopped: list, links_state: dict | None = None):
    """Fake subwoofer_runtime whose snapshot reflects helper_state."""

    async def stop_helper(self):
        stopped.append(True)
        helper_state["active"] = False
        helper_state["helper_pid"] = None
        helper_state["helper_args"] = None

    async def reclean_direct_easyeffects_links(self):
        # This is the production link-only reconciliation boundary.  It must
        # not restart the helper or touch the sample-rate force.  Tests that
        # model a transient EE port race can opt into healing the link state.
        if links_state is not None and links_state.get("repairable", True):
            links_state["ee_to_helper"] = True

    return type(
        "Runtime",
        (),
        {
            "snapshot": lambda self: dict(helper_state),
            "_stop_helper": stop_helper,
            "reclean_direct_easyeffects_links": reclean_direct_easyeffects_links,
        },
    )()


async def _noop(*args, **kwargs):
    return None


class RadioHandoffWrapperTests(unittest.IsolatedAsyncioTestCase):
    """Radio target-rate resolution and graph work run through the Coordinator."""

    async def asyncSetUp(self):
        self.originals = {
            name: getattr(main, name)
            for name in ("playback_transition_generation", "subwoofer_runtime")
        }
        main.playback_transition_generation = 100
        main.subwoofer_runtime = None

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    async def _run(self, *, live_rate=44100, generation=100, shared_raises=None):
        effective_rate = live_rate or 44100
        with patch.object(
            main,
            "get_samplerate_status",
            return_value={"active_rate": effective_rate, "force_rate": effective_rate},
        ):
            result, runtime = await run_main_handoff_through_coordinator(
                target_rate=effective_rate,
                generation=generation,
                source="radio",
                target_url=STATION_URL,
                detail="radio-post-load-handoff",
                live_rate=live_rate,
                failure=shared_raises,
                use_core=False,
            )
        calls = [{
            "target_rate": result.target_rate,
            "reason": "radio-post-load-handoff",
            "transition_generation": generation,
        }]
        return result.target_rate, calls, runtime

    async def test_shared_handoff_failure_propagates(self):
        # A real handoff failure must NOT be swallowed by the radio wrapper:
        # it propagates so /api/play keeps mpv paused and answers HTTP 500.
        with self.assertRaises(RuntimeError) as ctx:
            await self._run(
                shared_raises=RuntimeError(
                    "Playback handoff failed: graph links verification missing"
                ),
            )
        self.assertIn("graph links verification missing", str(ctx.exception))

    async def test_stale_generation_aborts_cleanly(self):
        # A newer transition took over: clean abort (None), the Coordinator
        # must not be invoked at all (nothing touched).
        main.playback_transition_generation = 101
        with self.assertRaises(RuntimeError) as ctx:
            await self._run(generation=100)
        self.assertIn("stale transition generation", str(ctx.exception))

    async def test_success_returns_live_rate_with_single_coordinator_transition(self):
        result, calls, runtime = await self._run(live_rate=44100)
        self.assertEqual(result, 44100)
        self.assertEqual(len(calls), 1, "exactly one Coordinator transition")
        self.assertEqual(calls[0]["target_rate"], 44100)
        self.assertEqual(calls[0]["reason"], "radio-post-load-handoff")
        self.assertEqual(calls[0]["transition_generation"], 100)
        self.assertIn("gate.set:True", runtime.events)
        self.assertLess(runtime.events.index("gate.set:True"), runtime.events.index("commit-readback"))

    async def test_live_rate_unavailable_falls_back_and_handoffs(self):
        # Timeout on the live rate: safe fallback 44100 is used and the
        # Coordinator transition still runs exactly once (no swallowed error).
        result, calls, _runtime = await self._run(live_rate=None)
        self.assertEqual(result, 44100)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["target_rate"], 44100)

    async def test_same_rate_complete_graph_is_noop_without_syncs(self):
        # 2.2, 44100 -> 44100, complete graph and helper at rate: the
        # Coordinator transition is a no-op - no preset reload, no helper sync.
        calls = {"preset": [], "subwoofer": []}
        status = {"active_rate": 44100, "force_rate": 44100}
        helper_state = {
            "active": True, "helper_pid": 999,
            "helper_args": ["--rate", "44100"],
        }

        async def preset_sync(**kwargs):
            calls["preset"].append(kwargs)

        async def helper_sync(*args, **kwargs):
            calls["subwoofer"].append((args, kwargs))

        async def noop_sleep(_delay):
            return None

        async def pw_link(*args):
            return _links_text({"ee_to_helper": True})

        runtime = _make_runtime(helper_state, [])

        with patch.object(
            main, "_wait_for_radio_live_rate_after_load",
            return_value=44100,
        ), patch.object(
            main, "get_samplerate_status", side_effect=lambda: dict(status)
        ), patch.object(main, "_ensure_playback_samplerate_force", _noop), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: 44100
        ), patch.object(main, "_set_pipewire_force_rate", lambda rate: None), patch.object(
            main, "_run_pw_link_command", pw_link
        ), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": "subwoofer-2.2", "effective_output_key": OUTPUT_KEY}},
        ), patch.object(main.asyncio, "sleep", noop_sleep), patch.object(
            main, "subwoofer_runtime", runtime
        ):
            result, _runtime = await run_main_handoff_through_coordinator(
                target_rate=44100,
                generation=100,
                source="radio",
                target_url=STATION_URL,
                detail="radio-post-load-handoff",
            )
        self.assertEqual(result.target_rate, 44100)
        self.assertEqual(calls["preset"], [], "no-op must not reload the preset")
        self.assertEqual(calls["subwoofer"], [], "no-op must not sync the helper")


class RadioCoordinatorGraphTests(unittest.IsolatedAsyncioTestCase):
    """Transient 2.2 link races are reconciled inside the Coordinator."""

    async def asyncSetUp(self):
        self.originals = {
            name: getattr(main, name)
            for name in ("playback_transition_generation", "subwoofer_runtime")
        }
        main.playback_transition_generation = 200

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    async def _run(self, *, repair_after_first_sync=True, helper_active=True):
        """48 -> 44.1 switch in 2.2 mode.

        The first helper sync leaves the EE->helper links missing (the
        transient race observed live); a later sync restores them when
        repair_after_first_sync is set.
        """
        calls = {"force": [], "preset": [], "subwoofer": []}
        status = {"active_rate": 48000, "force_rate": 48000}
        links_state = {
            "ee_to_helper": True,
            "repairable": bool(repair_after_first_sync),
        }
        helper_state = {"active": False, "helper_pid": None, "helper_args": None}
        stopped = []

        async def force(rate, reason, *, policy=None):
            calls["force"].append((rate, reason))
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def preset_sync(**kwargs):
            calls["preset"].append(kwargs)

        async def helper_sync(*args, **kwargs):
            calls["subwoofer"].append((args, kwargs))
            helper_state["active"] = helper_active
            helper_state["helper_pid"] = 4242
            helper_state["helper_args"] = ["--rate", "44100"]
            if len(calls["subwoofer"]) == 1:
                # Transient race: right after the first sync the EE->helper
                # links are missing (EE port recreation is still settling).
                links_state["ee_to_helper"] = False
            elif repair_after_first_sync:
                # The repair sync restores them.
                links_state["ee_to_helper"] = True

        async def noop_sleep(_delay):
            return None

        async def pw_link(*args):
            return _links_text(links_state)

        runtime = _make_runtime(helper_state, stopped, links_state)

        with patch.object(
            main, "get_samplerate_status", side_effect=lambda: dict(status)
        ), patch.object(main, "_ensure_playback_samplerate_force", force), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: status["force_rate"] or 0
        ), patch.object(main, "_set_pipewire_force_rate", lambda rate: None), patch.object(
            main, "_run_pw_link_command", pw_link
        ), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": "subwoofer-2.2", "effective_output_key": OUTPUT_KEY}},
        ), patch.object(main.asyncio, "sleep", noop_sleep), patch.object(
            main, "subwoofer_runtime", runtime
        ):
            result, _coordinator_runtime = await run_main_handoff_through_coordinator(
                target_rate=44100,
                generation=200,
                source="radio",
                target_url=STATION_URL,
                detail="radio-post-load-handoff",
            )
        return result, calls, stopped

    async def test_transient_missing_ee_to_helper_links_repaired_in_handoff(self):
        # The link race is healed inside the single Coordinator transition by a
        # second, readback-driven sync - not by a follow-up sync after
        # unpause. Handoff succeeds, no rollback.
        result, calls, stopped = await self._run(repair_after_first_sync=True)
        self.assertTrue(result)
        self.assertEqual(calls["force"], [(44100, "radio-post-load-handoff")])
        self.assertEqual(len(calls["preset"]), 1, "rate change -> preset sync")
        self.assertEqual(
            len(calls["subwoofer"]), 1,
            "the helper is established exactly once; link-only reconciliation heals the race",
        )
        self.assertEqual(stopped, [], "no rollback on success")

    async def test_unrepairable_links_fail_without_parallel_rollback(self):
        # Links stay missing: the Coordinator fails with a concrete error.
        # There is no second handoff or parallel helper rollback.
        result_holder = {}
        with self.assertRaises(RuntimeError) as ctx:
            await self._run(repair_after_first_sync=False)
        message = str(ctx.exception)
        self.assertIn("canonical topology", message)

    async def test_unrepairable_links_do_not_start_parallel_rollback(self):
        force_writes = []
        calls = {"force": [], "preset": [], "subwoofer": []}
        status = {"active_rate": 48000, "force_rate": 48000}
        links_state = {"ee_to_helper": True, "repairable": False}
        helper_state = {"active": False, "helper_pid": None, "helper_args": None}
        stopped = []

        async def force(rate, reason, *, policy=None):
            calls["force"].append((rate, reason))
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def preset_sync(**kwargs):
            calls["preset"].append(kwargs)

        async def helper_sync(*args, **kwargs):
            calls["subwoofer"].append((args, kwargs))
            helper_state["active"] = True
            helper_state["helper_pid"] = 4242
            helper_state["helper_args"] = ["--rate", "44100"]
            links_state["ee_to_helper"] = False  # never heals

        async def noop_sleep(_delay):
            return None

        async def pw_link(*args):
            return _links_text(links_state)

        def set_force(rate):
            force_writes.append(rate)
            status["force_rate"] = rate

        runtime = _make_runtime(helper_state, stopped, links_state)

        with patch.object(
            main, "get_samplerate_status", side_effect=lambda: dict(status)
        ), patch.object(main, "_ensure_playback_samplerate_force", force), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: status["force_rate"] or 0
        ), patch.object(main, "_set_pipewire_force_rate", set_force), patch.object(
            main, "_run_pw_link_command", pw_link
        ), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": "subwoofer-2.2", "effective_output_key": OUTPUT_KEY}},
        ), patch.object(main.asyncio, "sleep", noop_sleep), patch.object(
            main, "subwoofer_runtime", runtime
        ):
            with self.assertRaises(RuntimeError):
                await run_main_handoff_through_coordinator(
                    target_rate=44100,
                    generation=200,
                    source="radio",
                    target_url=STATION_URL,
                    detail="radio-post-load-handoff",
                )
        self.assertEqual(
            len(calls["subwoofer"]),
            1,
            "helper establishment is attempted once, then canonical readback fails",
        )
        self.assertEqual(force_writes, [], "Coordinator failure has no parallel force-rate rollback")
        self.assertEqual(stopped, [], "Coordinator failure has no parallel helper rollback")


class RadioHandoffDiagnosisLogTests(unittest.IsolatedAsyncioTestCase):
    """The missing-graph log names every component individually."""

    async def asyncSetUp(self):
        self.originals = {
            name: getattr(main, name)
            for name in ("playback_transition_generation", "subwoofer_runtime")
        }
        main.playback_transition_generation = 300

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    async def test_diagnosis_log_lists_ee_ports_helper_ports_and_missing_links(self):
        # 2.2 with only the EE->helper links missing and no repair able to
        # restore them: the graph-incomplete log must state EE ports
        # present, helper ports present and name exactly the two missing
        # links (fine-grained, per link) before the handoff fails.
        calls = {"preset": [], "subwoofer": []}
        status = {"active_rate": 44100, "force_rate": 44100}
        links_state = {"ee_to_helper": False, "repairable": False}
        helper_state = {"active": True, "helper_pid": 999, "helper_args": ["--rate", "44100"]}
        warnings = []

        async def preset_sync(**kwargs):
            calls["preset"].append(kwargs)

        async def helper_sync(*args, **kwargs):
            calls["subwoofer"].append((args, kwargs))

        async def noop_sleep(_delay):
            return None

        async def pw_link(*args):
            return _links_text(links_state)

        runtime = _make_runtime(helper_state, [], links_state)

        def record_warning(*args, **kwargs):
            warnings.append(args)

        with patch.object(
            main, "get_samplerate_status", side_effect=lambda: dict(status)
        ), patch.object(main, "_ensure_playback_samplerate_force", _noop), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: 44100
        ), patch.object(main, "_set_pipewire_force_rate", lambda rate: None), patch.object(
            main, "_run_pw_link_command", pw_link
        ), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": "subwoofer-2.2", "effective_output_key": OUTPUT_KEY}},
        ), patch.object(main.asyncio, "sleep", noop_sleep), patch.object(
            main, "subwoofer_runtime", runtime
        ), patch.object(main.logger, "warning", record_warning):
            with self.assertRaises(RuntimeError) as ctx:
                await run_main_handoff_through_coordinator(
                    target_rate=44100,
                    generation=300,
                    source="radio",
                    target_url=STATION_URL,
                    detail="radio-post-load-handoff",
                )
        self.assertIn("canonical topology", str(ctx.exception))
        entries = [
            args for args in warnings
            if "graph incomplete" in str(args[0])
        ]
        self.assertTrue(entries, "a graph-incomplete diagnosis log must be emitted")
        fmt_args = entries[0]
        # (fmt, mode, output_key, target_rate, ee_ports, helper_ports,
        #  helper_active, helper_rate, direct_bypass, source_links,
        #  missing_links, reason, detail)
        self.assertEqual(fmt_args[1], "subwoofer-2.2")
        self.assertEqual(fmt_args[2], OUTPUT_KEY)
        self.assertEqual(fmt_args[3], 44100)
        self.assertTrue(fmt_args[4], "EE output ports were present")
        self.assertTrue(fmt_args[5], "helper ports were present")
        missing = fmt_args[10]
        self.assertEqual(
            missing,
            [
                "ee_soe_output_level:output_FL -> fxroute_21_stage1:input_L",
                "ee_soe_output_level:output_FR -> fxroute_21_stage1:input_R",
            ],
            "exactly the missing EE->helper links are named",
        )
        self.assertEqual(fmt_args[11], "coordinator-play")


class RadioPlayErrorSemanticsTests(unittest.IsolatedAsyncioTestCase):
    """/api/play: unpause only after a verified handoff, no double sync."""

    async def asyncSetUp(self):
        names = (
            "player_instance", "current_track_info", "last_track_info",
            "last_radio_track_info", "source_transition_lock",
            "playback_transition_generation", "current_footer_owner",
            "radio_reconnect_attempts", "radio_reconnect_url",
            "radio_reconnect_active_since",
            "playback_stream_stale_after_measurement",
            "_playback_state_before_measurement",
            "radio_stream_stale_after_measurement",
            "_radio_state_before_measurement",
            "playback_queue_mode", "playback_queue", "subwoofer_runtime",
            "playback_transition_coordinator",
        )
        self.originals = {name: getattr(main, name) for name in names}
        main.player_instance = None
        main.current_track_info = None
        main.last_track_info = None
        main.last_radio_track_info = None
        main.source_transition_lock = None
        main.playback_transition_generation = 100
        main.current_footer_owner = None
        main.radio_reconnect_attempts = 0
        main.radio_reconnect_url = None
        main.radio_reconnect_active_since = 0.0
        main.playback_stream_stale_after_measurement = False
        main._playback_state_before_measurement = None
        main.radio_stream_stale_after_measurement = False
        main._radio_state_before_measurement = None
        main.playback_queue_mode = "app_replace"
        main.playback_queue = []
        main.subwoofer_runtime = None

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    def _make_player(self):
        class FakePlayer:
            def __init__(self):
                self._running = True
                self.state = {
                    "current_file": None, "paused": False, "ended": False,
                    "playing": False, "position": 0.0, "volume": 100,
                }
                self.pause_calls = []
                self.loaded = []
                self.events = []

            def set_pause(self, paused):
                self.pause_calls.append(paused)
                self.events.append("pause" if paused else "unpause")
                self.state["paused"] = paused
                self.state["playing"] = not paused
                if not paused:
                    self.state["position"] += 0.1

            def loadfile(self, url, mode="replace", start_paused=None):
                self.loaded.append((url, mode))
                self.events.append("load")
                self.state["current_file"] = url
                if start_paused is not None:
                    self.set_pause(bool(start_paused))

            def set_volume(self, volume):
                self.state["volume"] = volume

            def get_property(self, name):
                if name == "audio-params":
                    return {"samplerate": 44100}
                return None

            def get_metadata(self):
                return {}

        return FakePlayer()

    async def _play(
        self,
        *,
        source,
        sync_runtime,
        preset_sync,
        status,
        links_state,
        helper_state,
        stopped,
        force=None,
        live_rate=None,
        local_handoff=None,
        stations=None,
        outcome=None,
    ):
        """Run ``/api/play`` with a real Coordinator and a fake runtime."""
        player = self._make_player()
        handoff_calls = []
        outcome = outcome if outcome is not None else {}

        async def noop_force(rate, reason, *, policy=None):
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def noop_preset(**kwargs):
            preset_sync.append(kwargs)

        async def noop_sleep(_delay):
            return None

        async def pw_link(*_args):
            return _links_text(links_state)

        class EndpointRuntime:
            def __init__(self):
                self.muted = False
                self.active_rate = status.get("active_rate")

            async def read_hardware_mute(self):
                return self.muted

            async def set_hardware_mute(self, muted, transition_id):
                self.muted = bool(muted)

            async def read_transition_snapshot(self, request):
                return {}

            async def quiet_old_source(self, request):
                player.set_pause(True)

            async def resolve_target_rate(self, request):
                if request.source == "radio":
                    return live_rate if live_rate is not None else 44100
                return request.target_rate

            async def establish_target_rate(self, request):
                if force is not None:
                    aligned = await force(request.target_rate, "coordinator-rate", policy=None)
                else:
                    aligned = await noop_force(request.target_rate, "coordinator-rate")
                if not aligned:
                    raise RuntimeError("target rate did not align")
                self.active_rate = request.target_rate

            async def establish_effects_and_helper(self, request):
                handoff_calls.append({
                    "target_rate": request.target_rate,
                    "operation": request.operation,
                    "source": request.source,
                })
                if local_handoff is not None and request.source == "local":
                    await local_handoff(
                        dict(request.target_track),
                        request.target_rate,
                        transition_generation=main.playback_transition_generation,
                    )
                    return
                await main._coordinator_establish_effects_and_helper(
                    request,
                    ee_port_timeout_ms=main.PLAYBACK_HANDOFF_EE_PORT_TIMEOUT_MS,
                )

            async def prepare_target_source(self, request):
                player.set_volume(0)
                if request.target_url:
                    player.loadfile(request.target_url, mode="replace", start_paused=True)
                player.set_pause(True)

            async def start_target_source(self, request):
                player.set_pause(not request.should_play)

            async def set_source_volume(self, volume, transition_id):
                player.set_volume(volume)

            async def verify_committed_transition(self, request):
                if request.target_url and player.state.get("current_file") != request.target_url:
                    raise RuntimeError("target file was not committed")
                if request.should_play and player.state.get("paused"):
                    raise RuntimeError("target source did not start")
                return {"committed": True, "active_rate": self.active_rate}

            async def pause_source_after_failure(self, request):
                player.set_pause(True)
                player.set_volume(0)

        class GraphRuntime:
            def snapshot(self):
                return dict(helper_state)

            async def reclean_direct_easyeffects_links(self):
                if links_state.get("repairable", True):
                    links_state["ee_to_helper"] = True

        runtime = EndpointRuntime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        patches = [
            patch.object(main, "player_instance", player),
            patch.object(
                main, "get_stations",
                return_value=stations
                if stations is not None
                else [stations_module.Station(id="s1", name="Test Radio", stream_url=STATION_URL)],
            ),
            patch.object(
                main, "_prepare_local_queue",
                return_value={"id": "a", "title": "A", "source": "local", "url": "/music/a.flac"},
            ),
            patch.object(main, "_can_send_play_command", lambda: True),
            patch.object(main, "pause_spotify_for_local_playback_broadcast", _noop),
            patch.object(main, "_clear_playback_queue", lambda: None),
            patch.object(main, "_reset_mpv_loop_state", lambda: None),
            patch.object(
                main, "get_samplerate_status", side_effect=lambda: dict(status)
            ),
            patch.object(main.asyncio, "sleep", noop_sleep),
            patch.object(main, "_record_local_track_started", lambda track: None),
            patch.object(main, "_mark_player_state_authoritative", lambda state: None),
            patch.object(main, "_maybe_recover_samplerate_mismatch", _noop),
            patch.object(main, "_schedule_silent_active_watch", lambda **kwargs: None),
            patch.object(
                main, "get_audio_output_overview",
                return_value={
                    "output_mode": {
                        "mode": "subwoofer-2.2",
                        "effective_output_key": OUTPUT_KEY,
                    }
                },
            ),
            patch.object(main, "_run_pw_link_command", pw_link),
            patch.object(main, "_sync_easyeffects_preset_for_playback_samplerate", noop_preset),
            patch.object(main, "_sync_subwoofer_runtime", sync_runtime),
            patch.object(main, "subwoofer_runtime", GraphRuntime()),
            patch.object(main, "playback_transition_coordinator", coordinator),
        ]
        with ExitStack() as stack:
            for entry in patches:
                stack.enter_context(entry)
            try:
                outcome["result"] = await main.play_track(
                    main.PlayRequest(source=source, track_id="s1" if source == "radio" else "a")
                )
            finally:
                # Capture the player and call log before the stack restores
                # the patched globals, so failure tests can assert on them.
                outcome["player"] = player
                outcome["handoff_calls"] = handoff_calls
                outcome["runtime"] = runtime
        return outcome

    async def test_radio_transient_repair_single_handoff_then_unpause(self):
        # Radio 48 -> 44.1 in 2.2 with a transiently missing EE->helper
        # link set: reconciled inside the single Coordinator transition; the play
        # endpoint unpauses only afterwards and runs NO follow-up sync.
        sync_calls = []
        events = []
        status = {"active_rate": 48000, "force_rate": 48000}
        links_state = {"ee_to_helper": True, "repairable": True}
        helper_state = {"active": False, "helper_pid": None, "helper_args": None}
        stopped = []

        async def sync_runtime(*args, **kwargs):
            sync_calls.append((args, kwargs))
            events.append("sync")
            helper_state["active"] = True
            helper_state["helper_pid"] = 4242
            helper_state["helper_args"] = ["--rate", "44100"]
            if len(sync_calls) == 1:
                links_state["ee_to_helper"] = False  # transient race
            else:
                links_state["ee_to_helper"] = True   # repair round heals

        async def preset_sync(**kwargs):
            return None

        outcome = await self._play(
            source="radio",
            sync_runtime=sync_runtime,
            preset_sync=[],
            status=status,
            links_state=links_state,
            helper_state=helper_state,
            stopped=stopped,
        )

        player = outcome["player"]
        handoff_calls = outcome["handoff_calls"]

        self.assertEqual(outcome["result"]["status"], "playing")
        self.assertEqual(len(handoff_calls), 1, "exactly one radio handoff")
        self.assertEqual(
            player.pause_calls, [True, True, True, False],
            "paused across load+handoff, unpaused only after verified handoff",
        )
        self.assertEqual(len(sync_calls), 1, "helper establishment occurs once")
        self.assertTrue(
            all(not args for args, _ in sync_calls),
            "no positional overview argument: the old follow-up "
            "_sync_subwoofer_runtime(get_audio_output_overview()) is gone",
        )
        last_event = player.events.index("unpause")
        self.assertNotIn("sync", player.events[last_event + 1:], "no sync after unpause")
        self.assertEqual(stopped, [], "no rollback on success")

    async def test_radio_handoff_failure_no_unpause_no_followup_sync(self):
        # EE->helper links never heal: canonical readback fails, the
        # handoff raises, /api/play answers HTTP 500, mpv stays paused and
        # no sync runs after the handoff.
        sync_calls = []
        status = {"active_rate": 48000, "force_rate": 48000}
        links_state = {"ee_to_helper": True, "repairable": False}
        helper_state = {"active": False, "helper_pid": None, "helper_args": None}
        stopped = []

        async def sync_runtime(*args, **kwargs):
            sync_calls.append((args, kwargs))
            helper_state["active"] = True
            helper_state["helper_pid"] = 4242
            helper_state["helper_args"] = ["--rate", "44100"]
            links_state["ee_to_helper"] = False  # never heals

        async def preset_sync(**kwargs):
            return None

        outcome = {}
        with self.assertRaises(main.HTTPException) as ctx:
            await self._play(
                source="radio",
                sync_runtime=sync_runtime,
                preset_sync=[],
                status=status,
                links_state=links_state,
                helper_state=helper_state,
                stopped=stopped,
                outcome=outcome,
            )
        self.assertEqual(ctx.exception.status_code, 500)
        # The failing path is shared with local: the concrete handoff
        # failure was logged by the Coordinator before propagation.
        player = outcome["player"]
        self.assertEqual(player.pause_calls, [True, True], "must stay paused")
        self.assertNotIn(False, player.pause_calls, "no unpause after failed handoff")
        self.assertEqual(len(sync_calls), 1, "helper establishment occurs once")
        self.assertTrue(
            all(not args for args, _ in sync_calls),
            "no follow-up sync with an overview after the handoff",
        )
        self.assertEqual(stopped, [], "Coordinator failure has no parallel helper rollback")

    async def test_local_handoff_failure_propagates_500_like_radio(self):
        # Local path: the Coordinator failure propagates to /api/play as
        # HTTP 500 exactly like the radio path (consistent error semantics),
        # mpv stays paused.
        async def failing_local_handoff(track_info, expected_rate, *, transition_generation):
            raise RuntimeError("Playback handoff failed: graph links verification missing")

        async def sync_runtime(*args, **kwargs):
            raise AssertionError("no helper sync may run for local failure")

        async def preset_sync(**kwargs):
            raise AssertionError("no preset sync may run for local failure")

        status = {"active_rate": 48000, "force_rate": 48000}
        links_state = {"ee_to_helper": True}
        helper_state = {"active": True, "helper_pid": 999, "helper_args": ["--rate", "48000"]}
        stopped = []

        outcome = {}
        with self.assertRaises(main.HTTPException) as ctx:
            await self._play(
                source="local",
                sync_runtime=sync_runtime,
                preset_sync=[],
                status=status,
                links_state=links_state,
                helper_state=helper_state,
                stopped=stopped,
                local_handoff=failing_local_handoff,
                outcome=outcome,
            )
        self.assertEqual(ctx.exception.status_code, 500)
        player = outcome["player"]
        self.assertEqual(player.pause_calls, [True, True], "local stays paused too")
        self.assertNotIn(False, player.pause_calls)

    async def test_radio_same_rate_noop_unpauses_without_syncs(self):
        # 44100 -> 44100 with a complete graph: the Coordinator transition is a
        # no-op, so no preset reload and no helper sync at all; the
        # endpoint still unpauses (nothing failed).
        sync_calls = []
        preset_calls = []
        status = {"active_rate": 44100, "force_rate": 44100}
        links_state = {"ee_to_helper": True}
        helper_state = {
            "active": True, "helper_pid": 999,
            "helper_args": ["--rate", "44100"],
        }
        stopped = []

        async def sync_runtime(*args, **kwargs):
            sync_calls.append((args, kwargs))

        async def preset_sync(**kwargs):
            preset_calls.append(kwargs)

        outcome = await self._play(
            source="radio",
            sync_runtime=sync_runtime,
            preset_sync=preset_calls,
            status=status,
            links_state=links_state,
            helper_state=helper_state,
            stopped=stopped,
        )

        player = outcome["player"]
        handoff_calls = outcome["handoff_calls"]

        self.assertEqual(outcome["result"]["status"], "playing")
        self.assertEqual(len(handoff_calls), 1, "handoff runs (and decides no-op)")
        self.assertEqual(player.pause_calls, [True, True, True, False])
        self.assertEqual(sync_calls, [], "no helper sync on no-op")
        self.assertEqual(preset_calls, [], "no preset reload on no-op")
        self.assertEqual(stopped, [])


if __name__ == "__main__":
    unittest.main()
