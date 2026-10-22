#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Automatic source admission must outlive neither its signal nor its graph."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.source_feed import SourceOverviewFeed, SourceTransitionLock
from audio.source_transitions import SourceTransitionDependencies, SourceTransitions
from audio.samplerate import overview as source_overview


class AutomaticAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def make_transitions(self):
        self.valid = True
        self.selection = {"mode": "app-playback", "selected_input_key": None}
        self.events = []

        def save(mode, key):
            self.events.append(("save", mode))
            self.selection = {"mode": mode, "selected_input_key": key}
            return {**self.selection, "selected_input": {"key": key} if key else None}

        async def route(overview):
            self.events.append(("route", overview["mode"]))
            return overview

        self.deps = SourceTransitionDependencies(
            get_lock=lambda: self.lock, feed=SourceOverviewFeed(AsyncMock()),
            build_overview=lambda: dict(self.selection), save_selection=save,
            load_selection=lambda: dict(self.selection), with_current_stdin_status=lambda value: value,
            sync_external_input=route, sync_bluetooth_input=AsyncMock(side_effect=lambda value: value),
            get_stdin_input=lambda: None, note_source_selection=lambda mode: self.events.append(("commit", mode)),
            pause_app_playback=AsyncMock(side_effect=lambda: self.events.append(("stop", None))),
            sync_peak_monitor=AsyncMock(), sync_peak_monitor_for_stdin=AsyncMock(),
        )
        self.lock = SourceTransitionLock()
        self.deps.prepare_selection = lambda mode, key: {"mode": mode, "input_key": key}
        self.deps.commit_prepared_selection = lambda prepared: save(prepared["mode"], prepared["input_key"])
        return SourceTransitions(self.deps)

    def decide(self, selection):
        return ("external-input", "tv") if self.valid else None

    async def test_signal_is_rechecked_after_waiting_for_the_graph(self):
        transitions = self.make_transitions()

        async def wait():
            self.valid = False

        self.deps.wait_for_auto_switch = wait
        self.assertIsNone(await transitions.save_selection_if(self.decide))
        self.assertEqual(self.events, [], "expired admission must not touch selection or playback")

    async def test_slow_selection_preparation_cannot_route_an_expired_claim(self):
        transitions = self.make_transitions()

        def slow_prepare(mode, key):
            self.valid = False
            return {"mode": mode, "input_key": key}

        self.deps.prepare_selection = slow_prepare
        self.assertIsNone(await transitions.save_selection_if(self.decide))
        self.assertEqual(self.selection["mode"], "app-playback")
        self.assertEqual(self.events, [], "preparation must not persist even a temporary target")

    async def test_expired_claim_preserves_an_unavailable_previous_selection(self):
        transitions = self.make_transitions()
        self.selection = {"mode": "external-input", "selected_input_key": "gone"}
        save = self.deps.save_selection

        def validated_save(mode, key):
            if key == "gone":
                raise ValueError("Unknown input: gone")
            result = save(mode, key)
            self.valid = False
            return result

        def prepare(mode, key):
            self.valid = False
            return {"mode": mode, "input_key": key}

        self.deps.save_selection = validated_save
        self.deps.prepare_selection = prepare
        self.assertIsNone(await transitions.save_selection_if(self.decide))
        self.assertEqual(self.selection, {"mode": "external-input", "selected_input_key": "gone"})
        self.assertEqual(self.events, [])

    async def test_expiry_during_real_discovery_cannot_change_capture_ports(self):
        transitions = self.make_transitions()
        mutations = []

        def overview():
            self.valid = False
            return {"mode": "app-playback", "inputs": [
                {"key": "tv", "source_key": "capture", "port_key": "digital"}]}

        self.deps.save_selection = source_overview.set_audio_source_selection
        self.deps.prepare_selection = getattr(source_overview, "prepare_audio_source_selection",
                                             source_overview.set_audio_source_selection)
        with patch.object(source_overview, "get_audio_source_overview", side_effect=overview), patch.object(
            source_overview, "_set_source_port", side_effect=lambda *args: mutations.append(("port", args))
        ), patch.object(source_overview, "_save_audio_source_selection",
                        side_effect=lambda *args: mutations.append(("selection", args))):
            self.assertIsNone(await transitions.save_selection_if(self.decide))
        self.assertEqual(mutations, [], "expired discovery must not change the capture port or selection")

    async def test_valid_claim_still_routes_and_stops_app_playback(self):
        transitions = self.make_transitions()
        result = await transitions.save_selection_if(self.decide)
        self.assertEqual(result["mode"], "external-input")
        self.assertEqual(self.events, [("save", "external-input"), ("route", "external-input"),
                                       ("commit", "external-input"), ("stop", None)])


class ProductionGraphContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_guard_tracks_epoch_helper_and_graph_revision(self):
        import main
        snapshot = {"helper_pid": 10, "graph_revision": 3}
        dsp = SimpleNamespace(snapshot=lambda: dict(snapshot), sync_in_progress=False)
        with patch.object(main.runtime, "dsp_runtime", dsp), patch.object(
            main.playback_state, "playback_transition_epoch", 7
        ), patch.object(main, "_playback_transition_is_active", return_value=False):
            first, busy = main.input_auto_select._deps.graph_state()
            self.assertFalse(busy)
            snapshot["graph_revision"] += 1
            second, busy = main.input_auto_select._deps.graph_state()
            self.assertNotEqual(first, second)
            snapshot["helper_pid"] = 11
            third, _ = main.input_auto_select._deps.graph_state()
            self.assertNotEqual(second, third)
            main.playback_state.playback_transition_epoch += 1
            fourth, _ = main.input_auto_select._deps.graph_state()
            self.assertNotEqual(third, fourth)
            dsp.sync_in_progress = True
            self.assertTrue(main.input_auto_select._deps.graph_state()[1])

    async def test_production_guard_covers_queued_coordinator_work(self):
        import main
        with patch.object(main, "_playback_transition_is_active", return_value=True):
            self.assertTrue(main.input_auto_select._deps.graph_state()[1])


if __name__ == "__main__":
    unittest.main()
