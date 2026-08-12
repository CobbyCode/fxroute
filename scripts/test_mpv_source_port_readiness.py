#!/usr/bin/env python3
"""Focused MPV->EasyEffects source port-readiness contract tests.

Covers the bounded two-phase handoff in ``main._ensure_mpv_to_easyeffects_links``:

* cold radio: MPV ports absent for several read-only polls, appear later ->
  no premature link mutation, then only the missing edges are created;
* ports never appear -> bounded failure without any link mutation;
* ports already present -> no artificial delay, no unnecessary relinking;
* only missing edges are created, existing edges stay untouched;
* local playback shares the exact same prepare path (no radio-only copy).
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback_transition_test_support import make_transition_runtime


def _io_text(*, mpv: bool) -> str:
    lines = [
        "ee_soe_output_level:output_FL",
        "ee_soe_output_level:output_FR",
        "easyeffects_sink:playback_FL",
        "easyeffects_sink:playback_FR",
    ]
    if mpv:
        lines.extend(["mpv:output_FL", "mpv:output_FR"])
    return "\n".join(lines)


def _link_text(*, fl: bool = True, fr: bool = True) -> str:
    lines = [
        "ee_soe_output_level:output_FL -> fxroute_21_stage1:input_L",
        "ee_soe_output_level:output_FR -> fxroute_21_stage1:input_R",
    ]
    if fl:
        lines.append("mpv:output_FL -> easyeffects_sink:playback_FL")
    if fr:
        lines.append("mpv:output_FR -> easyeffects_sink:playback_FR")
    return "\n".join(lines)


def _connect_call(args: tuple) -> bool:
    return len(args) == 2 and args not in (("-io",), ("-l",))


class MpvSourcePortReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_radio_waits_read_only_then_links_without_premature_mutations(self):
        state = {"mpv_ports": False, "fl_linked": False, "fr_linked": False}
        calls = []
        premature = {"count": 0}

        async def pw_link(*args):
            calls.append(args)
            if args == ("-io",):
                return _io_text(mpv=state["mpv_ports"])
            if args == ("-l",):
                return _link_text(fl=state["fl_linked"], fr=state["fr_linked"])
            if args[0].startswith("mpv:") and not state["mpv_ports"]:
                premature["count"] += 1
            if args[1] == "easyeffects_sink:playback_FL":
                state["fl_linked"] = True
            elif args[1] == "easyeffects_sink:playback_FR":
                state["fr_linked"] = True
            return ""

        async def appear_later():
            await asyncio.sleep(0.35)
            state["mpv_ports"] = True

        appear = asyncio.create_task(appear_later())
        try:
            with patch.object(main, "_run_pw_link_command", side_effect=pw_link):
                result = await main._ensure_mpv_to_easyeffects_links(timeout_ms=2000)
        finally:
            await appear

        self.assertTrue(result, "links complete after the ports appeared")
        self.assertEqual(premature["count"], 0, "no link mutation before the MPV ports exist")
        io_polls = [c for c in calls if c == ("-io",)]
        self.assertGreater(len(io_polls), 1, "read-only polls covered the cold-start window")
        connects = [c for c in calls if _connect_call(c)]
        self.assertEqual(
            connects,
            [
                ("mpv:output_FL", "easyeffects_sink:playback_FL"),
                ("mpv:output_FR", "easyeffects_sink:playback_FR"),
            ],
            "exactly the two missing edges were created, once each",
        )

    async def test_ports_never_appear_fails_bounded_without_mutations(self):
        calls = []
        start = time.monotonic()

        async def pw_link(*args):
            calls.append(args)
            if args == ("-io",):
                return _io_text(mpv=False)
            if args == ("-l",):
                return _link_text(fl=False, fr=False)
            return ""

        with patch.object(main, "_run_pw_link_command", side_effect=pw_link):
            result = await main._ensure_mpv_to_easyeffects_links(timeout_ms=250)

        elapsed = time.monotonic() - start
        self.assertFalse(result, "bounded failure when the ports never appear")
        self.assertGreater(elapsed, 0.2, "the readiness budget was actually waited out")
        self.assertLess(elapsed, 2.0, "failure stays bounded")
        self.assertEqual(
            [c for c in calls if _connect_call(c)],
            [],
            "no link mutation while the source ports are absent",
        )
        self.assertNotIn(("-l",), calls, "no link readback before the ports exist")

    async def test_ready_ports_and_links_no_artificial_delay_no_relink(self):
        calls = []
        start = time.monotonic()

        async def pw_link(*args):
            calls.append(args)
            if args == ("-io",):
                return _io_text(mpv=True)
            if args == ("-l",):
                return _link_text(fl=True, fr=True)
            return ""

        with patch.object(main, "_run_pw_link_command", side_effect=pw_link):
            result = await main._ensure_mpv_to_easyeffects_links(timeout_ms=2000)

        elapsed = time.monotonic() - start
        self.assertTrue(result)
        self.assertLess(elapsed, 0.5, "no artificial delay when everything is ready")
        self.assertEqual(
            [c for c in calls if _connect_call(c)],
            [],
            "no unnecessary relinking for already-present edges",
        )

    async def test_only_missing_edges_are_created(self):
        state = {"fl_linked": True, "fr_linked": False}
        connects = []

        async def pw_link(*args):
            if args == ("-io",):
                return _io_text(mpv=True)
            if args == ("-l",):
                return _link_text(fl=state["fl_linked"], fr=state["fr_linked"])
            connects.append(args)
            if args == ("mpv:output_FR", "easyeffects_sink:playback_FR"):
                state["fr_linked"] = True
            return ""

        with patch.object(main, "_run_pw_link_command", side_effect=pw_link):
            result = await main._ensure_mpv_to_easyeffects_links(timeout_ms=2000)

        self.assertTrue(result)
        self.assertEqual(
            connects,
            [("mpv:output_FR", "easyeffects_sink:playback_FR")],
            "only the missing FR edge is created, the existing FL edge stays untouched",
        )


class SharedPreparePathTests(unittest.IsolatedAsyncioTestCase):
    """Local and radio must share the same MPV->EasyEffects prepare path."""

    class FakePlayer:
        def __init__(self):
            self._running = True
            self.state = {
                "current_file": None,
                "paused": True,
                "playing": False,
                "ended": False,
                "position": 0.0,
                "volume": 100,
                "playlist_pos": None,
            }
            self.calls = []

        def set_pause(self, paused):
            self.calls.append(("pause", bool(paused)))
            self.state["paused"] = bool(paused)
            self.state["playing"] = not bool(paused) and bool(self.state.get("current_file"))

        def loadfile(self, path, mode="replace", start_paused=None):
            self.calls.append(("loadfile", path, mode, start_paused))
            self.state["current_file"] = path
            self.state["paused"] = True if start_paused is None else bool(start_paused)
            self.state["playing"] = False

        def set_volume(self, volume):
            self.calls.append(("volume", volume))
            self.state["volume"] = volume

        def set_loop_playlist(self, enabled):
            self.calls.append(("loop-playlist", bool(enabled)))

        def set_shuffle(self, enabled):
            self.calls.append(("shuffle", bool(enabled)))

    async def _prepare(self, source: str, url: str):
        fake = self.FakePlayer()
        ensure = AsyncMock(return_value=True)
        runtime = make_transition_runtime()
        request = main.TransitionRequest(
            operation="play",
            source=source,
            target_url=url,
            target_rate=44100,
            should_play=True,
            rate_change=False,
            reload_source=True,
        )
        with patch.object(main, "player_instance", fake), patch.object(
            main, "_ensure_mpv_to_easyeffects_links", ensure
        ):
            await runtime.prepare_target_source(request)
        return fake, ensure

    async def test_local_prepare_uses_shared_ensure_path(self):
        fake, ensure = await self._prepare("local", "/music/current.flac")
        self.assertIn(("loadfile", "/music/current.flac", "replace", True), fake.calls)
        ensure.assert_awaited_once_with()

    async def test_radio_prepare_uses_shared_ensure_path(self):
        fake, ensure = await self._prepare("radio", "https://ice4.somafm.com/groovesalad-256-mp3")
        self.assertIn(
            ("loadfile", "https://ice4.somafm.com/groovesalad-256-mp3", "replace", True),
            fake.calls,
        )
        ensure.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
