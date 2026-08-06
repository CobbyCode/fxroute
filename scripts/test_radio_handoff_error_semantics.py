#!/usr/bin/env python3
"""REFACTOR-008: unified radio/local handoff error semantics.

Covers:
- _complete_radio_handoff_after_load propagates shared-handoff failures
  (no tolerant swallow), stale generations still abort cleanly
- transient link races (missing EE->helper links in 2.2) are repaired
  inside the shared handoff while mpv stays paused: exactly one handoff,
  readback-driven repair rounds, no blind sleeps
- /api/play: set_pause(False) only after a verified handoff; on failure
  no unpause, no follow-up _sync_subwoofer_runtime after the handoff
- local and radio failure semantics are consistent (error -> HTTP 500)
- no-op cases still run without helper/preset sync
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


def _make_runtime(helper_state: dict, stopped: list):
    """Fake subwoofer_runtime whose snapshot reflects helper_state."""

    async def stop_helper(self):
        stopped.append(True)
        helper_state["active"] = False
        helper_state["helper_pid"] = None
        helper_state["helper_args"] = None

    return type(
        "Runtime",
        (),
        {
            "snapshot": lambda self: dict(helper_state),
            "_stop_helper": stop_helper,
        },
    )()


async def _noop(*args, **kwargs):
    return None


class RadioHandoffWrapperTests(unittest.IsolatedAsyncioTestCase):
    """Direct tests of _complete_radio_handoff_after_load."""

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
        calls = []

        async def shared_handoff(**kwargs):
            calls.append(kwargs)
            if shared_raises is not None:
                raise shared_raises
            return True

        with patch.object(
            main, "_wait_for_radio_live_rate_after_load",
            return_value=live_rate,
        ), patch.object(main, "_complete_playback_handoff", shared_handoff):
            result = await main._complete_radio_handoff_after_load(
                {"source": "radio", "url": STATION_URL},
                48000,
                transition_generation=generation,
            )
        return result, calls

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
        # A newer transition took over: clean abort (None), the shared
        # handoff must not be invoked at all (nothing touched).
        main.playback_transition_generation = 101
        result, calls = await self._run(generation=100)
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    async def test_success_returns_live_rate_with_single_handoff(self):
        result, calls = await self._run(live_rate=44100)
        self.assertEqual(result, 44100)
        self.assertEqual(len(calls), 1, "exactly one shared handoff")
        self.assertEqual(calls[0]["target_rate"], 44100)
        self.assertEqual(calls[0]["reason"], "radio-post-load-handoff")
        self.assertEqual(calls[0]["transition_generation"], 100)

    async def test_live_rate_unavailable_falls_back_and_handoffs(self):
        # Timeout on the live rate: safe fallback 44100 is used and the
        # shared handoff still runs exactly once (no swallowed error).
        result, calls = await self._run(live_rate=None)
        self.assertEqual(result, 44100)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["target_rate"], 44100)

    async def test_same_rate_complete_graph_is_noop_without_syncs(self):
        # 2.2, 44100 -> 44100, complete graph and helper at rate: the shared
        # handoff is a no-op - no preset reload, no helper sync.
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
            result = await main._complete_radio_handoff_after_load(
                {"source": "radio", "url": STATION_URL},
                44100,
                transition_generation=100,
            )
        self.assertEqual(result, 44100)
        self.assertEqual(calls["preset"], [], "no-op must not reload the preset")
        self.assertEqual(calls["subwoofer"], [], "no-op must not sync the helper")


class RadioHandoffRepairTests(unittest.IsolatedAsyncioTestCase):
    """Transient link races are repaired inside the shared handoff (2.2)."""

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
        links_state = {"ee_to_helper": True}
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

        runtime = _make_runtime(helper_state, stopped)

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
            result = await main._complete_playback_handoff(
                target_rate=44100,
                reason="radio-post-load-handoff",
                transition_generation=200,
                detail=f"url={STATION_URL}",
            )
        return result, calls, stopped

    async def test_transient_missing_ee_to_helper_links_repaired_in_handoff(self):
        # The link race is healed inside the single shared handoff by a
        # second, readback-driven sync - not by a follow-up sync after
        # unpause. Handoff succeeds, no rollback.
        result, calls, stopped = await self._run(repair_after_first_sync=True)
        self.assertTrue(result)
        self.assertEqual(calls["force"], [(44100, "radio-post-load-handoff")])
        self.assertEqual(len(calls["preset"]), 1, "rate change -> preset sync")
        self.assertEqual(
            len(calls["subwoofer"]), 2,
            "initial sync + one bounded repair round, both inside the handoff",
        )
        self.assertEqual(stopped, [], "no rollback on success")

    async def test_unrepairable_links_fail_with_rollback(self):
        # Links stay missing after both bounded repair rounds: the handoff
        # fails with a concrete error and rolls back (force restored,
        # helper started by this handoff stopped). No silent success.
        result_holder = {}
        with self.assertRaises(RuntimeError) as ctx:
            await self._run(repair_after_first_sync=False)
        message = str(ctx.exception)
        self.assertIn("graph links verification missing", message)
        self.assertIn("44100", message)

    async def test_unrepairable_links_rollback_restores_force_and_stops_helper(self):
        force_writes = []
        calls = {"force": [], "preset": [], "subwoofer": []}
        status = {"active_rate": 48000, "force_rate": 48000}
        links_state = {"ee_to_helper": True}
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

        runtime = _make_runtime(helper_state, stopped)

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
                await main._complete_playback_handoff(
                    target_rate=44100,
                    reason="radio-post-load-handoff",
                    transition_generation=200,
                    detail=f"url={STATION_URL}",
                )
        self.assertEqual(
            len(calls["subwoofer"]),
            3,
            "initial sync + 2 bounded repair rounds, then fail",
        )
        self.assertEqual(force_writes, [48000], "previous force-rate restored")
        self.assertEqual(stopped, [True], "helper started by the handoff must stop")


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
        links_state = {"ee_to_helper": False}
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

        runtime = _make_runtime(helper_state, [])

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
                await main._complete_playback_handoff(
                    target_rate=44100,
                    reason="radio-post-load-handoff",
                    transition_generation=300,
                    detail=f"url={STATION_URL}",
                )
        self.assertIn("graph links verification missing", str(ctx.exception))
        entries = [
            args for args in warnings
            if "graph incomplete" in str(args[0])
        ]
        self.assertTrue(entries, "a graph-incomplete diagnosis log must be emitted")
        fmt_args = entries[0]
        # (fmt, mode, output_key, target_rate, ee_ports, helper_ports,
        #  missing_links, reason, detail)
        self.assertEqual(fmt_args[1], "subwoofer-2.2")
        self.assertEqual(fmt_args[2], OUTPUT_KEY)
        self.assertEqual(fmt_args[3], 44100)
        self.assertTrue(fmt_args[4], "EE output ports were present")
        self.assertTrue(fmt_args[5], "helper ports were present")
        missing = fmt_args[6]
        self.assertEqual(
            missing,
            [
                "ee_soe_output_level:output_FL -> fxroute_21_stage1:input_L",
                "ee_soe_output_level:output_FR -> fxroute_21_stage1:input_R",
            ],
            "exactly the missing EE->helper links are named",
        )
        self.assertEqual(fmt_args[7], "radio-post-load-handoff")


class RadioPlayErrorSemanticsTests(unittest.IsolatedAsyncioTestCase):
    """/api/play: unpause only after a verified handoff, no double sync."""

    async def asyncSetUp(self):
        names = (
            "player_instance", "current_track_info", "last_track_info",
            "last_radio_track_info", "source_transition_lock",
            "playback_transition_generation", "current_footer_owner",
            "radio_reconnect_attempts", "radio_reconnect_url",
            "radio_reconnect_active_since", "local_playback_handoff_completed_url",
            "local_playback_handoff_completed_rate",
            "playback_stream_stale_after_measurement",
            "_playback_state_before_measurement",
            "radio_stream_stale_after_measurement",
            "_radio_state_before_measurement",
            "playback_queue_mode", "playback_queue", "subwoofer_runtime",
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
        main.local_playback_handoff_completed_url = None
        main.local_playback_handoff_completed_rate = None
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
                }
                self.pause_calls = []
                self.loaded = []
                self.events = []

            def set_pause(self, paused):
                self.pause_calls.append(paused)
                self.events.append("pause" if paused else "unpause")
                self.state["paused"] = paused

            def loadfile(self, url, mode="replace"):
                self.loaded.append((url, mode))
                self.events.append("load")
                self.state["current_file"] = url

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
        """Run play_track with the full mock stack around the real handoff.

        sync_runtime: async fake for _sync_subwoofer_runtime (records calls
        and mutates links_state/helper_state).
        preset_sync: async fake for the EE preset sync (records calls).
        force: async fake for _ensure_playback_samplerate_force.
        local_handoff: async fake for _complete_local_playback_handoff
        (only reached in local tests).
        """
        player = self._make_player()
        handoff_calls = []
        original_radio_handoff = main._complete_radio_handoff_after_load
        outcome = outcome if outcome is not None else {}

        async def radio_handoff(track_info, previous_rate, *, transition_generation):
            handoff_calls.append((track_info, previous_rate, transition_generation))
            return await original_radio_handoff(
                track_info, previous_rate,
                transition_generation=transition_generation,
            )

        async def noop_force(rate, reason, *, policy=None):
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def noop_preset(**kwargs):
            preset_sync.append(kwargs)

        async def noop_sleep(_delay):
            return None

        async def pw_link(*args):
            return _links_text(links_state)

        runtime = _make_runtime(helper_state, stopped)

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
            patch.object(
                main, "_should_apply_hard_handoff_for_requested_play",
                return_value=(False, None),
            ),
            patch.object(main, "_reset_mpv_loop_state", lambda: None),
            patch.object(main, "_apply_hard_playback_handoff", _noop),
            patch.object(
                main, "_prearm_known_local_samplerate",
                return_value=(48000, 501) if source == "local" else (None, None),
            ),
            patch.object(main, "_release_local_samplerate_prearm", _noop),
            patch.object(
                main, "_complete_local_playback_handoff",
                local_handoff or _noop,
            ),
            patch.object(
                main, "_wait_for_radio_live_rate_after_load",
                return_value=live_rate if live_rate is not None else 44100,
            ),
            patch.object(
                main, "get_samplerate_status", side_effect=lambda: dict(status)
            ),
            patch.object(main, "_ensure_playback_samplerate_force", force or noop_force),
            patch.object(main, "_sync_easyeffects_preset_for_playback_samplerate", noop_preset),
            patch.object(main, "_sync_subwoofer_runtime", sync_runtime),
            patch.object(
                main, "_get_current_pipewire_force_rate", lambda: status["force_rate"] or 0
            ),
            patch.object(main, "_set_pipewire_force_rate", lambda rate: None),
            patch.object(main, "_run_pw_link_command", pw_link),
            patch.object(
                main, "get_audio_output_overview",
                return_value={"output_mode": {"mode": "subwoofer-2.2", "effective_output_key": OUTPUT_KEY}},
            ),
            patch.object(main.asyncio, "sleep", noop_sleep),
            patch.object(main, "subwoofer_runtime", runtime),
            patch.object(main, "_record_local_track_started", lambda track: None),
            patch.object(main, "_mark_player_state_authoritative", lambda state: None),
            patch.object(main, "_sync_peak_monitor_after_playback_transition", _noop),
            patch.object(main, "_maybe_recover_samplerate_mismatch", _noop),
            patch.object(main, "_sync_subwoofer_runtime_after_playback_transition", _noop),
            patch.object(main, "_schedule_silent_active_watch", lambda **kwargs: None),
            patch.object(main, "_complete_radio_handoff_after_load", radio_handoff),
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
        return outcome

    async def test_radio_transient_repair_single_handoff_then_unpause(self):
        # Radio 48 -> 44.1 in 2.2 with a transiently missing EE->helper
        # link set: repaired inside the single shared handoff; the play
        # endpoint unpauses only afterwards and runs NO follow-up sync.
        sync_calls = []
        events = []
        status = {"active_rate": 48000, "force_rate": 48000}
        links_state = {"ee_to_helper": True}
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
            player.pause_calls, [True, True, False],
            "paused across load+handoff, unpaused only after verified handoff",
        )
        self.assertEqual(len(sync_calls), 2, "initial sync + one repair round")
        self.assertTrue(
            all(not args for args, _ in sync_calls),
            "no positional overview argument: the old follow-up "
            "_sync_subwoofer_runtime(get_audio_output_overview()) is gone",
        )
        last_event = player.events.index("unpause")
        self.assertNotIn("sync", player.events[last_event + 1:], "no sync after unpause")
        self.assertEqual(stopped, [], "no rollback on success")

    async def test_radio_handoff_failure_no_unpause_no_followup_sync(self):
        # EE->helper links never heal: bounded repair rounds fail, the
        # handoff raises, /api/play answers HTTP 500, mpv stays paused and
        # no sync runs after the handoff.
        sync_calls = []
        status = {"active_rate": 48000, "force_rate": 48000}
        links_state = {"ee_to_helper": True}
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
        # failure was logged by the shared handoff before propagation.
        player = outcome["player"]
        self.assertEqual(player.pause_calls, [True, True], "must stay paused")
        self.assertNotIn(False, player.pause_calls, "no unpause after failed handoff")
        self.assertEqual(
            len(sync_calls), 3,
            "initial sync + 2 bounded repair rounds, all inside the handoff",
        )
        self.assertTrue(
            all(not args for args, _ in sync_calls),
            "no follow-up sync with an overview after the handoff",
        )
        self.assertEqual(stopped, [True], "helper started by the handoff rolled back")

    async def test_local_handoff_failure_propagates_500_like_radio(self):
        # Local path: the shared-handoff failure propagates to /api/play as
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
        # 44100 -> 44100 with a complete graph: the shared handoff is a
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
        self.assertEqual(player.pause_calls, [True, True, False])
        self.assertEqual(sync_calls, [], "no helper sync on no-op")
        self.assertEqual(preset_calls, [], "no preset reload on no-op")
        self.assertEqual(stopped, [])


if __name__ == "__main__":
    unittest.main()
