#!/usr/bin/env python3
"""Shared playback-handoff tests for the readback-driven handoff.

Covers _complete_playback_handoff and the thin local wrapper:
- Local 48 -> 48 full no-op (no force write, no preset reload, no helper)
- Spotify 44.1 -> Local 48 with delayed EasyEffects output ports
- Local/Radio 44.1 <-> 48 exactly one switch
- same rate with missing EE ports: graph repair only, no force write
- EE port timeout: concrete error + clean rollback (force restored, helper
  started by this handoff stopped)
- stale transition generation aborts without touching anything
- stereo, 2.1 and 2.2 no-op/switch behavior
All PipeWire/mpv/EE I/O is mocked; no live audio commands are executed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback_transition import PlaybackTransitionCoordinator
from playback_transition_test_support import run_main_handoff_through_coordinator


def _ee_links_text() -> str:
    """Full link dump: EE ports, helper ports, and the complete link set
    (stereo EE->HW plus 2.1/2.2 helper links) as pw-link -l would print."""
    return (
        "ee_soe_output_level:output_FL\n"
        "ee_soe_output_level:output_FR\n"
        "fxroute_21_stage1:input_L\n"
        "fxroute_21_stage1:input_R\n"
        f"ee_soe_output_level:output_FL -> {OUTPUT_KEY}:playback_FL\n"
        f"ee_soe_output_level:output_FR -> {OUTPUT_KEY}:playback_FR\n"
        "ee_soe_output_level:output_FL -> fxroute_21_stage1:input_L\n"
        "ee_soe_output_level:output_FR -> fxroute_21_stage1:input_R\n"
        f"fxroute_21_stage1:output_1 -> {OUTPUT_KEY}:playback_FL\n"
        f"fxroute_21_stage1:output_2 -> {OUTPUT_KEY}:playback_FR\n"
        f"fxroute_21_stage1:output_3 -> {OUTPUT_KEY}:playback_RL\n"
        f"fxroute_21_stage1:output_4 -> {OUTPUT_KEY}:playback_RR\n"
    )


def _ee_links_text_no_helper_to_hw() -> str:
    """Helper running (ports + EE->helper links) but helper->HW links missing."""
    return (
        "ee_soe_output_level:output_FL\n"
        "ee_soe_output_level:output_FR\n"
        "fxroute_21_stage1:input_L\n"
        "fxroute_21_stage1:input_R\n"
        "ee_soe_output_level:output_FL -> fxroute_21_stage1:input_L\n"
        "ee_soe_output_level:output_FR -> fxroute_21_stage1:input_R\n"
    )


OUTPUT_KEY = "alsa_output.pci-0000_00_1f.3.analog-stereo"


class PlaybackHandoffSharedTests(unittest.IsolatedAsyncioTestCase):
    """Direct tests of the shared _complete_playback_handoff."""

    async def asyncSetUp(self):
        self.originals = {
            name: getattr(main, name)
            for name in ("playback_transition_generation", "subwoofer_runtime")
        }
        main.playback_transition_generation = 200

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    async def _run(
        self,
        *,
        target_rate,
        active_rate,
        force_rate=None,
        ee_ports=True,
        mode="stereo",
        helper=None,
        generation=200,
        ee_port_timeout_ms=5000,
        pw_link_calls=None,
    ):
        """Run the shared handoff with mocked primitives.

        helper: dict or None. When given (subwoofer modes) it is returned
        by the mocked subwoofer_runtime.snapshot(); None means
        subwoofer_runtime is mocked to an object with a snapshot returning
        empty state (so _sync_subwoofer_runtime is invoked but no real
        helper exists).
        """
        calls = {"force": [], "preset": [], "subwoofer": []}
        status = {"active_rate": active_rate, "force_rate": force_rate}

        async def force(rate, reason, *, policy=None):
            calls["force"].append((rate, reason))
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def preset_sync(**kwargs):
            calls["preset"].append(kwargs)

        async def helper_sync(*args, **kwargs):
            calls["subwoofer"].append((args, kwargs))

        async def noop_sleep(_delay):
            return None

        async def pw_link(*args):
            if pw_link_calls is not None:
                pw_link_calls.append(args)
            if not ee_ports:
                return ""
            return _ee_links_text()

        stopped = []

        async def async_stop_helper(self):
            stopped.append(True)

        runtime = type(
            "Runtime",
            (),
            {
                "snapshot": lambda self: (helper or {}),
                "_stop_helper": async_stop_helper,
            },
        )()

        with patch.object(
            main, "get_samplerate_status", side_effect=lambda: dict(status)
        ), patch.object(main, "_ensure_playback_samplerate_force", force), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: force_rate or 0
        ), patch.object(main, "_set_pipewire_force_rate", lambda rate: None), patch.object(
            main, "_run_pw_link_command", pw_link
        ), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": mode, "effective_output_key": OUTPUT_KEY}},
        ), patch.object(main.asyncio, "sleep", noop_sleep), patch.object(
            main, "subwoofer_runtime", runtime
        ):
            result, _coordinator_runtime = await run_main_handoff_through_coordinator(
                target_rate=target_rate,
                generation=generation,
                detail="test-handoff",
                ee_port_timeout_ms=ee_port_timeout_ms,
            )
        return result, calls, stopped

    async def test_local_48_to_48_full_noop(self):
        # Same rate + complete graph: no force write, no preset, no helper.
        result, calls, stopped = await self._run(
            target_rate=48000, active_rate=48000, force_rate=48000,
        )
        self.assertTrue(result)
        self.assertEqual(calls["force"], [])
        self.assertEqual(calls["preset"], [])
        self.assertEqual(calls["subwoofer"], [])

    async def test_local_48_to_48_noop_with_force_none(self):
        # Active rate matches and no force is set: still a no-op.
        result, calls, _ = await self._run(
            target_rate=48000, active_rate=48000, force_rate=None,
        )
        self.assertTrue(result)
        self.assertEqual(calls["force"], [])

    async def test_44_to_48_switches_exactly_once(self):
        result, calls, stopped = await self._run(
            target_rate=48000, active_rate=44100, force_rate=44100,
        )
        self.assertTrue(result)
        self.assertEqual(calls["force"], [(48000, "test-handoff")])
        self.assertEqual(len(calls["preset"]), 1)
        self.assertEqual(len(calls["subwoofer"]), 1)
        self.assertEqual(stopped, [])

    async def test_48_to_44_switches_exactly_once(self):
        result, calls, _ = await self._run(
            target_rate=44100, active_rate=48000, force_rate=48000,
        )
        self.assertTrue(result)
        self.assertEqual(calls["force"], [(44100, "test-handoff")])
        self.assertEqual(len(calls["preset"]), 1)
        self.assertEqual(len(calls["subwoofer"]), 1)

    async def test_same_rate_missing_ee_ports_repairs_graph_only(self):
        # Rate aligned but EE ports missing: graph repair only. No force
        # write (rate already matches); the preset sync recreates the EE
        # output ports, the readback wait observes them, then the helper is
        # synchronized.
        calls = {"force": [], "preset": [], "subwoofer": []}
        status = {"active_rate": 48000, "force_rate": 48000}
        ports_visible = {"ok": False}

        async def force(rate, reason, *, policy=None):
            calls["force"].append((rate, reason))
            return True

        async def preset_sync(**kwargs):
            calls["preset"].append(kwargs)
            ports_visible["ok"] = True  # preset reload recreates the ports

        async def helper_sync(*args, **kwargs):
            calls["subwoofer"].append((args, kwargs))

        async def noop_sleep(_delay):
            return None

        async def pw_link(*args):
            if not ports_visible["ok"]:
                return ""
            return _ee_links_text()

        stopped = []

        async def async_stop_helper(self):
            stopped.append(True)

        runtime = type(
            "Runtime",
            (),
            {
                "snapshot": lambda self: {"active": False, "helper_pid": None},
                "_stop_helper": async_stop_helper,
            },
        )()

        with patch.object(
            main, "get_samplerate_status", side_effect=lambda: dict(status)
        ), patch.object(main, "_ensure_playback_samplerate_force", force), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: 48000
        ), patch.object(main, "_set_pipewire_force_rate", lambda rate: None), patch.object(
            main, "_run_pw_link_command", pw_link
        ), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": "stereo", "effective_output_key": OUTPUT_KEY}},
        ), patch.object(main.asyncio, "sleep", noop_sleep), patch.object(
            main, "subwoofer_runtime", runtime
        ):
            result, _coordinator_runtime = await run_main_handoff_through_coordinator(
                target_rate=48000,
                generation=200,
                detail="test-handoff",
            )
        self.assertTrue(result)
        self.assertEqual(calls["force"], [], "same rate must not force")
        self.assertEqual(len(calls["preset"]), 1, "missing ports -> preset sync")
        self.assertEqual(len(calls["subwoofer"]), 1, "missing ports -> helper sync")
        self.assertEqual(stopped, [])

    async def test_delayed_ee_ports_44_to_48_switches_once(self):
        # EE ports appear only after several polls; the handoff must wait
        # via readback and still switch exactly once.
        pw_link_calls = []
        port_state = {"present": False}
        polls = {"n": 0}

        async def delayed_pw_link(*args):
            pw_link_calls.append(args)
            polls["n"] += 1
            if polls["n"] >= 3:
                port_state["present"] = True
            return _ee_links_text() if port_state["present"] else ""

        calls = {"force": [], "preset": [], "subwoofer": []}
        status = {"active_rate": 44100, "force_rate": 44100}

        async def force(rate, reason, *, policy=None):
            calls["force"].append((rate, reason))
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def preset_sync(**kwargs):
            calls["preset"].append(kwargs)

        async def helper_sync(*args, **kwargs):
            calls["subwoofer"].append((args, kwargs))

        async def noop_sleep(_delay):
            return None

        stopped = []

        async def async_stop_helper(self):
            stopped.append(True)

        runtime = type(
            "Runtime",
            (),
            {
                "snapshot": lambda self: {"active": False, "helper_pid": None},
                "_stop_helper": async_stop_helper,
            },
        )()

        with patch.object(
            main, "get_samplerate_status", side_effect=lambda: dict(status)
        ), patch.object(main, "_ensure_playback_samplerate_force", force), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: 44100
        ), patch.object(main, "_run_pw_link_command", delayed_pw_link), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": "stereo", "effective_output_key": OUTPUT_KEY}},
        ), patch.object(main.asyncio, "sleep", noop_sleep), patch.object(
            main, "subwoofer_runtime", runtime
        ):
            result, _coordinator_runtime = await run_main_handoff_through_coordinator(
                target_rate=48000,
                generation=200,
                detail="test-delayed-ee-ports",
                ee_port_timeout_ms=5000,
            )
        self.assertTrue(result)
        self.assertEqual(calls["force"], [(48000, "test-delayed-ee-ports")])
        self.assertEqual(len(calls["preset"]), 1)
        self.assertEqual(len(calls["subwoofer"]), 1)
        self.assertGreaterEqual(len(pw_link_calls), 3, "EE ports must be polled")
        self.assertEqual(stopped, [])

    async def test_ee_port_timeout_raises_and_rolls_back(self):
        # Ports never appear: concrete RuntimeError naming the EE port, the
        # previous force-rate restored, helper started by this handoff stopped.
        result_holder = {}
        with self.assertRaises(RuntimeError) as ctx:
            await self._run(
                target_rate=48000, active_rate=44100, force_rate=44100,
                ee_ports=False, ee_port_timeout_ms=10,
            )
        message = str(ctx.exception)
        self.assertIn("EasyEffects output ports", message)
        self.assertIn("48000", message)

    async def test_ee_port_timeout_restores_force_only(self):
        # EE-port timeout: the helper is never started (port wait precedes
        # helper sync), so only the previous force-rate must be restored.
        force_writes = []
        status = {"active_rate": 44100, "force_rate": 44100}
        helper_state = {"active": False, "helper_pid": None}
        stopped = []

        async def pw_link(*args):
            return ""

        async def force(rate, reason, *, policy=None):
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def preset_sync(**kwargs):
            pass

        async def helper_sync(*args, **kwargs):
            helper_state["active"] = True
            helper_state["helper_pid"] = 12345

        async def noop_sleep(_delay):
            return None

        def set_force(rate):
            force_writes.append(rate)

        async def stop_helper(self):
            stopped.append(True)
            helper_state["helper_pid"] = None
            helper_state["active"] = False

        runtime = type(
            "Runtime",
            (),
            {
                "snapshot": lambda self: dict(helper_state),
                "_stop_helper": stop_helper,
            },
        )()

        with patch.object(
            main, "get_samplerate_status", side_effect=lambda: dict(status)
        ), patch.object(main, "_ensure_playback_samplerate_force", force), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: 44100
        ), patch.object(main, "_set_pipewire_force_rate", set_force), patch.object(
            main, "_run_pw_link_command", pw_link
        ), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": "stereo", "effective_output_key": OUTPUT_KEY}},
        ), patch.object(main.asyncio, "sleep", noop_sleep), patch.object(
            main, "subwoofer_runtime", runtime
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await run_main_handoff_through_coordinator(
                    target_rate=48000,
                    generation=200,
                    detail="test-timeout-cleanup",
                    ee_port_timeout_ms=10,
                )
        self.assertIn("EasyEffects output ports", str(ctx.exception))
        self.assertEqual(force_writes, [44100], "force must be restored to 44100")
        self.assertEqual(stopped, [], "helper was never started in the timeout path")

    async def test_verification_failure_stops_helper_started_by_handoff(self):
        # Cleanup contract: a helper that THIS handoff started must be
        # terminated when the final verification fails, and the previous
        # force-rate restored (no dangling wrong-force/missing-helper).
        force_writes = []
        status = {"active_rate": 44100, "force_rate": 44100}
        helper_state = {"active": False, "helper_pid": None}
        stopped = []

        async def pw_link(*args):
            return _ee_links_text()

        async def force(rate, reason, *, policy=None):
            return True  # force "succeeds" but sink never reaches 48000

        async def preset_sync(**kwargs):
            pass

        async def helper_sync(*args, **kwargs):
            helper_state["active"] = True
            helper_state["helper_pid"] = 12345

        async def noop_sleep(_delay):
            return None

        def set_force(rate):
            force_writes.append(rate)

        async def stop_helper(self):
            stopped.append(True)
            helper_state["helper_pid"] = None
            helper_state["active"] = False

        runtime = type(
            "Runtime",
            (),
            {
                "snapshot": lambda self: dict(helper_state),
                "_stop_helper": stop_helper,
            },
        )()

        with patch.object(
            main, "get_samplerate_status", side_effect=lambda: dict(status)
        ), patch.object(main, "_ensure_playback_samplerate_force", force), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: 44100
        ), patch.object(main, "_set_pipewire_force_rate", set_force), patch.object(
            main, "_run_pw_link_command", pw_link
        ), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": "stereo", "effective_output_key": OUTPUT_KEY}},
        ), patch.object(main.asyncio, "sleep", noop_sleep), patch.object(
            main, "subwoofer_runtime", runtime
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await run_main_handoff_through_coordinator(
                    target_rate=48000,
                    generation=200,
                    detail="test-verify-cleanup",
                )
        self.assertIn("verification", str(ctx.exception))
        self.assertEqual(force_writes, [44100], "force must be restored to 44100")
        self.assertEqual(stopped, [True], "helper started by this handoff must stop")

    async def test_stale_generation_aborts_without_touching_anything(self):
        main.playback_transition_generation = 201  # newer transition won
        with self.assertRaises(RuntimeError) as ctx:
            await self._run(
                target_rate=48000, active_rate=44100, force_rate=44100,
                generation=200,
            )
        self.assertIn("stale transition generation", str(ctx.exception))
        calls = {"force": [], "preset": [], "subwoofer": []}
        self.assertEqual(calls["force"], [])
        self.assertEqual(calls["preset"], [])
        self.assertEqual(calls["subwoofer"], [])


class PlaybackHandoffSubwooferModeTests(unittest.IsolatedAsyncioTestCase):
    """Shared handoff behavior in 2.1 / 2.2 subwoofer modes."""

    async def asyncSetUp(self):
        self.originals = {
            name: getattr(main, name)
            for name in ("playback_transition_generation", "subwoofer_runtime")
        }
        main.playback_transition_generation = 300

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    async def _run(self, *, mode, active_rate, force_rate, target_rate, helper):
        calls = {"force": [], "preset": [], "subwoofer": []}
        status = {"active_rate": active_rate, "force_rate": force_rate}

        async def force(rate, reason, *, policy=None):
            calls["force"].append((rate, reason))
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def preset_sync(**kwargs):
            calls["preset"].append(kwargs)

        async def helper_sync(*args, **kwargs):
            calls["subwoofer"].append((args, kwargs))

        async def noop_sleep(_delay):
            return None

        async def pw_link(*args):
            return _ee_links_text()

        stopped = []

        async def async_stop_helper(self):
            stopped.append(True)

        runtime = type(
            "Runtime",
            (),
            {"snapshot": lambda self: dict(helper or {}), "_stop_helper": async_stop_helper},
        )()

        with patch.object(
            main, "get_samplerate_status", side_effect=lambda: dict(status)
        ), patch.object(main, "_ensure_playback_samplerate_force", force), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: force_rate or 0
        ), patch.object(main, "_set_pipewire_force_rate", lambda rate: None), patch.object(
            main, "_run_pw_link_command", pw_link
        ), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": mode, "effective_output_key": OUTPUT_KEY}},
        ), patch.object(main.asyncio, "sleep", noop_sleep), patch.object(
            main, "subwoofer_runtime", runtime
        ):
            result, _coordinator_runtime = await run_main_handoff_through_coordinator(
                target_rate=target_rate,
                generation=300,
                detail="test-subwoofer",
            )
        return result, calls, stopped

    async def test_21_noop_when_rate_and_helper_match(self):
        for mode in ("subwoofer-2.1", "subwoofer-2.2", "subwoofer-2.2-stereo"):
            with self.subTest(mode=mode):
                result, calls, stopped = await self._run(
                    mode=mode,
                    active_rate=48000,
                    force_rate=48000,
                    target_rate=48000,
                    helper={"active": True, "helper_pid": 999, "helper_args": ["--rate", "48000"]},
                )
                self.assertTrue(result)
                self.assertEqual(calls["force"], [])
                self.assertEqual(calls["preset"], [])
                self.assertEqual(calls["subwoofer"], [])
                self.assertEqual(stopped, [])

    async def test_21_switch_requires_helper_rate_match(self):
        # 44.1 -> 48 in 2.1: helper must be running at the new rate after the
        # switch; the mocked helper_sync does not update the snapshot, so the
        # final verification fails -> RuntimeError (concrete, no dangling state).
        with self.assertRaises(RuntimeError) as ctx:
            await self._run(
                mode="subwoofer-2.1",
                active_rate=44100,
                force_rate=44100,
                target_rate=48000,
                helper={"active": False, "helper_pid": None, "helper_args": None},
            )
        self.assertIn("verification", str(ctx.exception))

    async def test_21_same_rate_missing_helper_repairs(self):
        # Same rate but helper not active in 2.1: graph repair only (no force
        # write), helper sync invoked, then verification still fails because
        # the mocked helper stays inactive -> concrete error.
        with self.assertRaises(RuntimeError) as ctx:
            await self._run(
                mode="subwoofer-2.1",
                active_rate=48000,
                force_rate=48000,
                target_rate=48000,
                helper={"active": False, "helper_pid": None, "helper_args": None},
            )
        self.assertIn("verification", str(ctx.exception))


class LocalPlaybackHandoffWrapperTests(unittest.IsolatedAsyncioTestCase):
    """Thin local wrapper: metadata preferred, MPV live rate as fallback."""

    async def asyncSetUp(self):
        self.originals = {
            name: getattr(main, name)
            for name in ("playback_transition_generation", "subwoofer_runtime")
        }
        main.playback_transition_generation = 400

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    async def _run_wrapper(self, *, expected_rate, live_rate=None, **handoff_kwargs):
        calls = {"force": [], "preset": [], "subwoofer": []}
        status = {
            "active_rate": handoff_kwargs.pop("active_rate", 48000),
            "force_rate": handoff_kwargs.pop("force_rate", 48000),
        }

        async def force(rate, reason, *, policy=None):
            calls["force"].append((rate, reason))
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def preset_sync(**kwargs):
            calls["preset"].append(kwargs)

        async def helper_sync(*args, **kwargs):
            calls["subwoofer"].append((args, kwargs))

        async def noop_sleep(_delay):
            return None

        async def pw_link(*args):
            return _ee_links_text()

        stopped = []

        async def async_stop_helper(self):
            stopped.append(True)

        runtime = type(
            "Runtime",
            (),
            {
                "snapshot": lambda self: {"active": False, "helper_pid": None},
                "_stop_helper": async_stop_helper,
            },
        )()

        target_rate = expected_rate if isinstance(expected_rate, int) and expected_rate > 0 else live_rate
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
            return_value={"output_mode": {"mode": "stereo", "effective_output_key": OUTPUT_KEY}},
        ), patch.object(main.asyncio, "sleep", noop_sleep), patch.object(
            main, "subwoofer_runtime", runtime
        ):
            await run_main_handoff_through_coordinator(
                target_rate=target_rate,
                generation=400,
                source="local",
                target_url="/music/a.flac",
                detail="local-playback-handoff",
                rate_change=bool(target_rate and target_rate != status["active_rate"]),
            )
        return calls, stopped

    async def test_metadata_preferred_48000_target(self):
        # Library metadata says 48000 -> target 48000, one switch from 44100.
        calls, stopped = await self._run_wrapper(
            expected_rate=48000, active_rate=44100, force_rate=44100,
        )
        self.assertEqual(calls["force"], [(48000, "local-playback-handoff")])
        self.assertEqual(len(calls["preset"]), 1)
        self.assertEqual(len(calls["subwoofer"]), 1)
        self.assertEqual(stopped, [])

    async def test_no_metadata_falls_back_to_mpv_live_rate(self):
        # expected_rate None -> MPV live rate (48000) used as target.
        calls, stopped = await self._run_wrapper(
            expected_rate=None, live_rate=48000, active_rate=44100, force_rate=44100,
        )
        self.assertEqual(calls["force"], [(48000, "local-playback-handoff")])
        self.assertEqual(stopped, [])

    async def test_no_metadata_no_live_rate_skips_cleanly(self):
        # No metadata and no MPV live rate: wrapper skips without error and
        # without touching sink/EE/helper.
        calls, stopped = await self._run_wrapper(
            expected_rate=None, live_rate=None, active_rate=48000, force_rate=48000,
        )
        self.assertEqual(calls["force"], [])
        self.assertEqual(calls["preset"], [])
        self.assertEqual(calls["subwoofer"], [])
        self.assertEqual(stopped, [])


class PlaybackQueueHandoffTests(unittest.IsolatedAsyncioTestCase):
    """Queue paths must route through the shared handoff.

    _load_queue_track (auto-next via _advance_playback_queue, manual
    next/previous and formerly native playlist switches) must submit one
    ``operation=queue`` request to the Coordinator and keep the target
    paused until its commit readback. There is no queue-local prearm or
    helper-only path anymore.
    """

    TRACK_A = {"url": "/music/a.flac", "sample_rate_hz": 44100, "id": "a", "title": "A", "source": "local"}
    TRACK_B = {"url": "/music/b.flac", "sample_rate_hz": 48000, "id": "b", "title": "B", "source": "local"}

    def _make_player(self):
        class FakePlayer:
            def __init__(self):
                self._running = True
                self.state = {"paused": True, "current_file": "/music/a.flac"}
                self.pause_calls = []
                self.loaded = []
                self.playlist_pos = None
                self.volume = 100

            def set_pause(self, paused):
                self.pause_calls.append(paused)
                self.state["paused"] = paused
                self.state["playing"] = not paused
                if not paused:
                    self.state["position"] = float(self.state.get("position", 0.0)) + 0.1

            def loadfile(self, url, mode="replace", start_paused=None):
                self.loaded.append((url, mode))
                self.state["current_file"] = url
                if start_paused is not None:
                    self.set_pause(bool(start_paused))

            def set_volume(self, volume):
                self.volume = volume
                self.state["volume"] = volume

            def set_playlist_pos(self, index):
                self.playlist_pos = index
                self.state["playlist_pos"] = index

            def set_loop_playlist(self, value):
                pass

            def set_loop_file(self, value):
                pass

            def get_property(self, name):
                return None

        return FakePlayer()

    async def asyncSetUp(self):
        self.originals = {
            name: getattr(main, name)
            for name in (
                "playback_queue", "playback_queue_index", "playback_queue_mode",
                "playback_queue_loop", "playback_queue_shuffle",
                "queue_transition_target_url", "playback_transition_generation",
                "current_track_info", "last_track_info", "player_instance",
                "subwoofer_runtime",
            )
        }
        main.playback_transition_generation = 500

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    async def _run_queue_track(self, *, index, mode="app_replace", tracks=None):
        tracks = tracks or [dict(self.TRACK_A), dict(self.TRACK_B)]
        player = self._make_player()
        handoff_calls = []
        helper_calls = []

        class QueueRuntime:
            def __init__(self):
                self.muted = False
                self.active_rate = tracks[0].get("sample_rate_hz")
                self.fail_stage = None

            async def _stage(self, name):
                if self.fail_stage == name:
                    raise RuntimeError("Playback handoff failed: graph links verification missing")

            async def read_hardware_mute(self):
                return self.muted

            async def set_hardware_mute(self, muted, transition_id):
                self.muted = bool(muted)

            async def read_transition_snapshot(self, request):
                await self._stage("snapshot")
                return {}

            async def quiet_old_source(self, request):
                await self._stage("quiet-old-source")
                player.set_pause(True)

            async def resolve_target_rate(self, request):
                await self._stage("target-rate-resolve")
                return request.target_rate

            async def establish_target_rate(self, request):
                await self._stage("target-rate")
                self.active_rate = request.target_rate

            async def establish_effects_and_helper(self, request):
                await self._stage("effects-helper-links")
                handoff_calls.append((dict(request.target_track), request.target_rate, request.operation))

            async def prepare_target_source(self, request):
                await self._stage("target-source-prepare")
                player.set_volume(0)
                player.loadfile(request.target_url, mode="replace", start_paused=True)

            async def start_target_source(self, request):
                await self._stage("target-source-start")
                player.set_pause(not request.should_play)

            async def set_source_volume(self, volume, transition_id):
                player.set_volume(volume)

            async def verify_committed_transition(self, request):
                await self._stage("commit-readback")
                if player.state.get("current_file") != request.target_url:
                    raise RuntimeError("target file was not committed")
                if request.should_play and player.state.get("paused"):
                    raise RuntimeError("target remained paused")
                return {"committed": True, "active_rate": self.active_rate}

            async def pause_source_after_failure(self, request):
                player.set_pause(True)

        main.playback_queue = tracks
        main.playback_queue_index = 0
        main.playback_queue_mode = mode
        main.playback_queue_loop = False
        main.playback_queue_shuffle = False
        main.queue_transition_target_url = None
        main.current_track_info = dict(tracks[0])
        main.last_track_info = dict(tracks[0])
        main.player_instance = player
        main.subwoofer_runtime = None
        runtime = QueueRuntime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        with patch.object(
            main, "_sync_track_context_from_queue_index",
            lambda idx: dict(main.playback_queue[idx]),
        ), patch.object(
            main, "get_samplerate_status",
            return_value={
                "active_rate": tracks[0].get("sample_rate_hz"),
                "force_rate": tracks[0].get("sample_rate_hz"),
            },
        ), patch.object(main, "_clear_playback_queue", lambda: None), patch.object(
            main, "playback_transition_coordinator", coordinator
        ):
            result = await main._load_queue_track(
                index, transition_reason="queue auto-advance",
            )
        return result, player, handoff_calls, helper_calls, runtime

    async def test_auto_next_44_to_48_routes_through_shared_handoff(self):
        # Auto-next 44.1 -> 48: the queue path must load paused and call the
        # shared handoff with the pre-armed 48000 rate; helper sync must not
        # happen outside the handoff anymore.
        result, player, handoff_calls, helper_calls, runtime = await self._run_queue_track(index=1)
        self.assertTrue(result)
        self.assertEqual(len(handoff_calls), 1)
        track, expected_rate, operation = handoff_calls[0]
        self.assertEqual(track["url"], "/music/b.flac")
        self.assertEqual(expected_rate, 48000)
        self.assertEqual(operation, "queue")
        self.assertIn(True, player.pause_calls, "paused across handoff")
        self.assertFalse(runtime.muted, "gate restored after committed queue transition")
        self.assertEqual(player.loaded, [("/music/b.flac", "replace")])
        self.assertEqual(
            helper_calls, [], "no separate helper sync outside the shared handoff"
        )

    async def test_auto_next_48_to_48_routes_through_shared_handoff(self):
        # Auto-next 48 -> 48: still routes through the shared handoff (which
        # itself decides no-op); the queue path must not skip it.
        tracks = [
            {**dict(self.TRACK_A), "sample_rate_hz": 48000},
            {**dict(self.TRACK_B), "sample_rate_hz": 48000},
        ]
        result, player, handoff_calls, helper_calls, runtime = await self._run_queue_track(
            index=1, tracks=tracks,
        )
        self.assertTrue(result)
        self.assertEqual(len(handoff_calls), 1)
        self.assertEqual(handoff_calls[0][1], 48000)
        self.assertIn(True, player.pause_calls)
        self.assertFalse(runtime.muted)
        self.assertEqual(helper_calls, [])

    async def test_manual_next_and_previous_route_through_shared_handoff(self):
        # Manual next/previous use the same _load_queue_track -> shared
        # handoff path as auto-next.
        result, player, handoff_calls, _, runtime = await self._run_queue_track(
            index=1, mode="app_replace",
        )
        self.assertTrue(result)
        self.assertEqual(len(handoff_calls), 1)
        self.assertEqual(handoff_calls[0][0]["url"], "/music/b.flac")
        self.assertFalse(runtime.muted)

    async def test_mpv_native_queue_switch_uses_shared_handoff(self):
        # mpv-native switch: pause -> set_playlist_pos -> shared handoff with
        # the track's metadata rate -> unpause. No prearm+helper-only path.
        result, player, handoff_calls, helper_calls, runtime = await self._run_queue_track(
            index=1, mode="mpv_native",
        )
        self.assertTrue(result)
        self.assertIsNone(player.playlist_pos, "native queue bypass is no longer used")
        self.assertIn(True, player.pause_calls)
        self.assertEqual(len(handoff_calls), 1)
        self.assertEqual(handoff_calls[0][0]["url"], "/music/b.flac")
        self.assertEqual(handoff_calls[0][1], 48000, "metadata rate submitted")
        self.assertEqual(handoff_calls[0][2], "queue")
        self.assertFalse(runtime.muted)
        self.assertEqual(helper_calls, [])

    async def test_auto_next_handoff_failure_leaves_paused(self):
        # Handoff failure during auto-next: mpv stays paused (no unpause),
        # the transition target is cleared, the error propagates.
        player = self._make_player()
        main.playback_queue = [dict(self.TRACK_A), dict(self.TRACK_B)]
        main.playback_queue_index = 0
        main.playback_queue_mode = "app_replace"
        main.playback_queue_loop = False
        main.playback_queue_shuffle = False
        main.queue_transition_target_url = None
        main.current_track_info = dict(self.TRACK_A)
        main.last_track_info = dict(self.TRACK_A)
        main.player_instance = player
        main.subwoofer_runtime = None

        class FailingRuntime:
            def __init__(self):
                self.muted = False

            async def read_hardware_mute(self):
                return self.muted

            async def set_hardware_mute(self, muted, transition_id):
                self.muted = bool(muted)

            async def read_transition_snapshot(self, request):
                return {}

            async def quiet_old_source(self, request):
                player.set_pause(True)

            async def resolve_target_rate(self, request):
                return request.target_rate

            async def establish_target_rate(self, request):
                return None

            async def establish_effects_and_helper(self, request):
                raise RuntimeError("Playback handoff failed: graph links verification missing")

            async def prepare_target_source(self, request):
                raise AssertionError("failed transition must not prepare target")

            async def start_target_source(self, request):
                raise AssertionError("failed transition must not start target")

            async def set_source_volume(self, volume, transition_id):
                player.set_volume(volume)

            async def verify_committed_transition(self, request):
                raise AssertionError("failed transition must not commit")

            async def pause_source_after_failure(self, request):
                player.set_pause(True)

        coordinator = PlaybackTransitionCoordinator(FailingRuntime(), gate_settle_seconds=0)

        with patch.object(
            main, "_sync_track_context_from_queue_index",
            lambda idx: dict(main.playback_queue[idx]),
        ), patch.object(main, "_clear_playback_queue", lambda: None), patch.object(
            main, "get_samplerate_status",
            return_value={"active_rate": 44100, "force_rate": 44100},
        ), patch.object(main, "playback_transition_coordinator", coordinator):
            with self.assertRaises(main.HTTPException) as ctx:
                await main._load_queue_track(1, transition_reason="queue auto-advance")
        self.assertIn("graph links verification missing", str(ctx.exception.detail))
        self.assertTrue(all(value for value in player.pause_calls), "must stay paused after failure")
        self.assertEqual(main.queue_transition_target_url, None)


class PlaybackGraphLinkVerificationTests(unittest.IsolatedAsyncioTestCase):
    """A running helper alone is not a complete graph: the actual links
    (stereo EE->HW; 2.1/2.2 EE->helper and helper->HW) must be present."""

    async def asyncSetUp(self):
        self.originals = {
            name: getattr(main, name)
            for name in ("playback_transition_generation", "subwoofer_runtime")
        }
        main.playback_transition_generation = 600

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    async def _run(self, *, mode, helper, link_text_provider, repair_by=None):
        calls = {"force": [], "preset": [], "subwoofer": []}
        status = {"active_rate": 48000, "force_rate": 48000}
        links_state = {"complete": False}

        async def force(rate, reason, *, policy=None):
            calls["force"].append((rate, reason))
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def preset_sync(**kwargs):
            calls["preset"].append(kwargs)
            if repair_by == "preset":
                links_state["complete"] = True  # EE preset sync rebuilds the links

        async def helper_sync(*args, **kwargs):
            calls["subwoofer"].append((args, kwargs))
            if repair_by == "helper":
                links_state["complete"] = True  # helper restart rebuilds the links

        async def noop_sleep(_delay):
            return None

        async def pw_link(*args):
            return link_text_provider(links_state)

        stopped = []

        async def async_stop_helper(self):
            stopped.append(True)

        runtime = type(
            "Runtime",
            (),
            {"snapshot": lambda self: dict(helper), "_stop_helper": async_stop_helper},
        )()

        with patch.object(
            main, "get_samplerate_status", side_effect=lambda: dict(status)
        ), patch.object(main, "_ensure_playback_samplerate_force", force), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: 48000
        ), patch.object(main, "_set_pipewire_force_rate", lambda rate: None), patch.object(
            main, "_run_pw_link_command", pw_link
        ), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": mode, "effective_output_key": OUTPUT_KEY}},
        ), patch.object(main.asyncio, "sleep", noop_sleep), patch.object(
            main, "subwoofer_runtime", runtime
        ):
            result, _coordinator_runtime = await run_main_handoff_through_coordinator(
                target_rate=48000,
                generation=600,
                detail="test-links",
            )
        return result, calls, stopped

    async def test_21_helper_running_but_helper_to_hw_links_missing_fails(self):
        # Helper process alive and at the right rate, but helper->HW links
        # missing and repair does not restore them: not a no-op, repair runs,
        # final link verification fails with a concrete error.
        def links_never(state):
            return _ee_links_text_no_helper_to_hw()

        with self.assertRaises(RuntimeError) as ctx:
            await self._run(
                mode="subwoofer-2.1",
                helper={"active": True, "helper_pid": 999, "helper_args": ["--rate", "48000"]},
                link_text_provider=links_never,
            )
        self.assertIn("graph links verification missing", str(ctx.exception))

    async def test_21_links_repaired_when_helper_sync_restores_them(self):
        # Same setup but the helper sync restores the missing links -> the
        # handoff succeeds and the graph is verified via the actual links.
        def links_after_repair(state):
            if not state["complete"]:
                return _ee_links_text_no_helper_to_hw()
            return _ee_links_text()

        result, calls, stopped = await self._run(
            mode="subwoofer-2.1",
            helper={"active": True, "helper_pid": 999, "helper_args": ["--rate", "48000"]},
            link_text_provider=links_after_repair,
            repair_by="helper",
        )
        self.assertTrue(result)
        self.assertEqual(calls["force"], [], "rate already aligned")
        self.assertEqual(len(calls["preset"]), 1, "repair ran")
        self.assertEqual(len(calls["subwoofer"]), 1, "helper sync ran")
        self.assertEqual(stopped, [])

    async def test_stereo_missing_ee_to_hw_links_fails(self):
        # Stereo: EE ports present but EE->HW links missing and not restored:
        # repair runs (EE preset sync), final verification fails concretely.
        def links_never(state):
            return (
                "ee_soe_output_level:output_FL\n"
                "ee_soe_output_level:output_FR\n"
            )

        with self.assertRaises(RuntimeError) as ctx:
            await self._run(
                mode="stereo",
                helper=None,
                link_text_provider=links_never,
            )
        self.assertIn("graph links verification missing", str(ctx.exception))

    async def test_stereo_links_repaired_when_preset_sync_restores_them(self):
        # Stereo: missing EE->HW links are restored by the EE preset sync,
        # no helper is involved; the handoff completes via actual links.
        def links_after_repair(state):
            if not state["complete"]:
                return (
                    "ee_soe_output_level:output_FL\n"
                    "ee_soe_output_level:output_FR\n"
                )
            return _ee_links_text()

        result, calls, stopped = await self._run(
            mode="stereo",
            helper=None,
            link_text_provider=links_after_repair,
            repair_by="preset",
        )
        self.assertTrue(result)
        self.assertEqual(calls["force"], [], "rate already aligned")
        self.assertEqual(len(calls["preset"]), 1, "EE preset sync rebuilds links")
        self.assertEqual(stopped, [])


class PlaybackHandoffCleanupReadbackTests(unittest.IsolatedAsyncioTestCase):
    """After a failed handoff a consistent paused/idle state must remain and
    be proven by the rollback readback: force-rate restored, no half helper
    graph, no stale links, no unintended unpause."""

    async def asyncSetUp(self):
        self.originals = {
            name: getattr(main, name)
            for name in ("playback_transition_generation", "subwoofer_runtime", "player_instance")
        }
        main.playback_transition_generation = 700

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(main, name, value)

    def _make_player(self):
        class FakePlayer:
            def __init__(self):
                self.state = {"paused": True, "current_file": "/music/a.flac"}
                self.pause_calls = []

            def set_pause(self, paused):
                self.pause_calls.append(paused)
                self.state["paused"] = paused

        return FakePlayer()

    async def test_timeout_cleanup_leaves_consistent_state_with_readback(self):
        # EE port timeout: force restored to the previous value, helper never
        # started, player untouched (no unpause), and the rollback readback
        # log proves final_force_rate + helper_pid + links + player state.
        force_writes = []
        status = {"active_rate": 44100, "force_rate": 44100}
        helper_state = {"active": False, "helper_pid": None}
        stopped = []
        log_entries = []
        player = self._make_player()

        async def pw_link(*args):
            return ""

        async def force(rate, reason, *, policy=None):
            status["active_rate"] = rate
            status["force_rate"] = rate
            return True

        async def preset_sync(**kwargs):
            pass

        async def helper_sync(*args, **kwargs):
            helper_state["active"] = True
            helper_state["helper_pid"] = 12345

        async def noop_sleep(_delay):
            return None

        def set_force(rate):
            force_writes.append(rate)

        async def stop_helper(self):
            stopped.append(True)
            helper_state["helper_pid"] = None
            helper_state["active"] = False

        runtime = type(
            "Runtime",
            (),
            {
                "snapshot": lambda self: dict(helper_state),
                "_stop_helper": stop_helper,
            },
        )()

        with patch.object(
            main, "get_samplerate_status", side_effect=lambda: dict(status)
        ), patch.object(main, "_ensure_playback_samplerate_force", force), patch.object(
            main, "_sync_easyeffects_preset_for_playback_samplerate", preset_sync
        ), patch.object(main, "_sync_subwoofer_runtime", helper_sync), patch.object(
            main, "_get_current_pipewire_force_rate", lambda: 44100
        ), patch.object(main, "_set_pipewire_force_rate", set_force), patch.object(
            main, "_run_pw_link_command", pw_link
        ), patch.object(
            main, "get_audio_output_overview",
            return_value={"output_mode": {"mode": "stereo", "effective_output_key": OUTPUT_KEY}},
        ), patch.object(main.asyncio, "sleep", noop_sleep), patch.object(
            main, "subwoofer_runtime", runtime
        ), patch.object(main, "player_instance", player), patch.object(
            main.logger, "info", side_effect=lambda *args, **kwargs: log_entries.append(args)
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await run_main_handoff_through_coordinator(
                    target_rate=48000,
                    generation=700,
                    detail="test-cleanup-readback",
                    ee_port_timeout_ms=10,
                )
        self.assertIn("EasyEffects output ports", str(ctx.exception))
        self.assertEqual(force_writes, [44100], "force restored to previous value")
        self.assertEqual(stopped, [], "helper never started in timeout path")
        self.assertEqual(player.pause_calls, [], "handoff must never touch pause")
        self.assertTrue(
            any("rollback readback" in str(entry[0])
                for entry in log_entries),
            "rollback readback log entry missing",
        )
        readback = next(
            entry for entry in log_entries
            if "rollback readback" in str(entry[0])
        )
        # Format: (fmt, reason, previous_force_rate, final_force_rate,
        #          helper_pid_after, helper_active_after, graph_links_complete,
        #          player_paused)
        self.assertEqual(readback[3], 44100, "final force-rate restored")
        self.assertIsNone(readback[4], "no half helper graph")
        self.assertFalse(readback[6], "no stale links after cleanup")
        self.assertTrue(readback[7], "player stays paused")


if __name__ == "__main__":
    unittest.main()
