#!/usr/bin/env python3
"""Regression tests: playback orchestrator sync reads stay off the event loop.

The samplerate/measurement responsiveness pass removed the blocking
pipewire/`pactl` pipelines from the playback hot paths, but the coordinator
entry points in ``playback/orchestration.py`` still ran the sync overview and
samplerate-status builders directly on the asyncio thread:

* ``transition_sample_rate_policy`` rebuilt the whole output overview and the
  4-command samplerate status inline,
* ``playback_graph_diagnosis`` rebuilt the overview inline whenever a
  transition did not already carry one,
* ``establish_effects_and_helper`` did the same for its fallback overview and
  for the failure-path status readback,
* ``abort_failed_transition`` -> ``_build_restore_request`` read the live
  samplerate status inline.

These tests pin the fix: wherever the orchestrator awaits these builders they
must execute inside a worker thread, never on the thread running the event
loop, and the surrounding behavior must stay unchanged.
"""

import asyncio
import sys
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playback.orchestration import PlaybackOrchestrationDeps, PlaybackOrchestrator
from playback.runtime.snapshot import _RuntimeSnapshotMixin
from playback.transition import TransitionRequest

MAIN_THREAD = threading.current_thread()


def _probe(self, seen, label):
    """Return a spy that fails if it runs on the event-loop thread."""

    def probe(*args, **kwargs):
        seen.append((label, threading.current_thread() is MAIN_THREAD))
        self.assertNotEqual(
            threading.current_thread(),
            MAIN_THREAD,
            f"{label} ran on the event-loop thread",
        )
        return None

    return probe


def _orchestrator(overrides) -> PlaybackOrchestrator:
    """Minimal PlaybackOrchestrationDeps; unused members point at stubs."""
    stub = lambda *a, **k: None  # noqa: E731

    async def _none(*a, **k):
        return None

    deps = PlaybackOrchestrationDeps(
        get_coordinator=lambda: None,
        set_coordinator=stub,
        make_transition_coordinator=stub,
        begin_transition_attempt=lambda: 1,
        end_transition_attempt=stub,
        run_transition=_none,
        get_playback_state=lambda: SimpleNamespace(
            current_track_info={}, latest_qobuz_state={}, current_playback_owner=None
        ),
        get_runtime_player=lambda: None,
        get_dsp_runtime=lambda: None,
        get_dsp_manager=lambda: None,
        get_dsp_preset_load_lock=lambda: None,
        get_measurement_session=lambda: None,
        get_samplerate_status=lambda: {},
        get_audio_output_overview=lambda: {"output_mode": {}},
        get_spotify_ui_state=_none,
        get_player_audio_samplerate=lambda: None,
        is_local_playback_active=lambda _state: False,
        is_spotify_playback_active=lambda _state: False,
        spotify_target_track=lambda _state: {},
        sample_rate_policy_is_auto=lambda: True,
        get_player_queue_fields=lambda: {},
        run_pw_link_command=_none,
        connect_ports=_none,
        contains_link=lambda _text, _s, _t: False,
        helper_argument_sample_rate=lambda _snapshot: None,
        sync_preset_for_samplerate=_none,
        sync_runtime=_none,
        reconcile_sink_rate=_none,
        load_dsp_preset=_none,
        sleep=lambda _delay: asyncio.sleep(0),
        pipewire_poll_interval_ms=10,
        dsp_port_timeout_ms=5,
        post_start_readbacks=2,
        output_mode_subwoofer_modes=frozenset(),
        output_mode_stereo="stereo",
    )
    for name, value in overrides.items():
        deps = replace(deps, **{name: value})
    return PlaybackOrchestrator(deps)


class PlaybackOrchestrationOffloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_transition_sample_rate_policy_reads_overview_and_status_off_loop(self):
        seen = []
        overview = _probe(self, seen, "overview")
        status = _probe(self, seen, "status")

        def overview_builder():
            overview()
            return {
                "output_mode": {"mode": "stereo", "effective_output_key": "test"},
                "selected_output": {"supported_rates": [44100]},
                "current_output": {},
            }

        def status_builder():
            status()
            return {"active_rate": 44100, "force_rate": 44100}

        run_transition = AsyncMock()
        orchestrator = _orchestrator(
            {
                "get_audio_output_overview": overview_builder,
                "get_samplerate_status": status_builder,
                "run_transition": run_transition,
            }
        )
        await orchestrator.transition_sample_rate_policy(
            {"mode": "fixed", "rate": 44100}, detail="test"
        )
        self.assertEqual(run_transition.await_count, 1)
        request = run_transition.await_args.args[0]
        self.assertEqual(request.operation, "sample-rate-policy")
        self.assertEqual(request.source, "local")
        self.assertEqual(request.target_rate, 44100)
        self.assertFalse(request.rate_change)
        labels = [label for label, _ in seen]
        self.assertIn("overview", labels)
        self.assertIn("status", labels)
        self.assertFalse(
            any(on_loop for _, on_loop in seen),
            "transition_sample_rate_policy ran overview/status builders on the event loop",
        )

    async def test_playback_graph_diagnosis_builds_overview_off_loop_when_not_provided(self):
        seen = []
        overview = _probe(self, seen, "overview")

        def overview_builder():
            overview()
            return {"output_mode": {"mode": "stereo", "effective_output_key": ""}}

        orchestrator = _orchestrator({"get_audio_output_overview": overview_builder})
        result = await orchestrator.playback_graph_diagnosis(
            None, source="radio", target_rate=44100
        )
        self.assertEqual(result["mode"], "stereo")
        self.assertEqual(seen, [("overview", False)])

    async def test_establish_effects_and_helper_fallback_overview_off_loop(self):
        seen = []
        overview = _probe(self, seen, "overview")

        def overview_builder():
            overview()
            return {"output_mode": {"mode": "stereo", "effective_output_key": ""}}

        async def pw_link_command(*args):
            raise RuntimeError("pw-link unavailable")

        orchestrator = _orchestrator(
            {
                "get_audio_output_overview": overview_builder,
                "run_pw_link_command": pw_link_command,
                "sync_preset_for_samplerate": AsyncMock(),
            }
        )
        request = TransitionRequest(
            operation="measurement-entry",
            source="radio",
            target_rate=44100,
            audio_overview=None,
        )
        with self.assertRaisesRegex(RuntimeError, "native DSP output ports"):
            await orchestrator.establish_effects_and_helper(request)
        self.assertEqual(seen, [("overview", False)])

    async def test_establish_effects_and_helper_failure_status_read_off_loop(self):
        seen = []
        status = _probe(self, seen, "status")

        def status_builder():
            status()
            return {"active_rate": 48000, "force_rate": 0}

        async def pw_link_command(*args):
            return "fxroute_dsp:input_1 fxroute_dsp:input_2 fxroute_dsp:output_1 fxroute_dsp:output_2"

        orchestrator = _orchestrator(
            {
                "get_audio_output_overview": lambda: {
                    "output_mode": {"mode": "stereo", "effective_output_key": ""}
                },
                "get_samplerate_status": status_builder,
                "run_pw_link_command": pw_link_command,
                "sync_preset_for_samplerate": AsyncMock(),
                "reconcile_sink_rate": AsyncMock(return_value=False),
            }
        )
        request = TransitionRequest(
            operation="measurement-entry",
            source="radio",
            target_rate=44100,
            audio_overview=None,
        )
        with self.assertRaisesRegex(RuntimeError, "expected=44100 active=48000 force=0"):
            await orchestrator.establish_effects_and_helper(request)
        self.assertEqual(seen, [("status", False)])

    async def test_abort_failed_transition_reads_restore_status_off_loop(self):
        seen = []
        status = _probe(self, seen, "status")

        def status_builder():
            status()
            return {"active_rate": 44100, "force_rate": 44100}

        class _Deps:
            @staticmethod
            def queue():
                return SimpleNamespace(native_request_fields=lambda: {})

            @staticmethod
            def coordinator_target_rate(source, track):
                return 44100

            get_samplerate_status = staticmethod(status_builder)

        class _Adapter(_RuntimeSnapshotMixin):
            def __init__(self):
                self._deps = _Deps()
                self._player = SimpleNamespace(state={})
                self._staged_target_url = None

        request = TransitionRequest(operation="radio-play", source="radio")
        snapshot = {
            "current_track": {"source": "radio", "url": "http://x"},
            "player": {"current_file": "http://x", "playing": True},
        }
        result = await _Adapter().abort_failed_transition(
            request, snapshot, target_staged=False
        )
        self.assertIsNotNone(result)
        restore = result["restore"]
        self.assertEqual(restore.operation, "replay")
        self.assertEqual(restore.target_rate, 44100)
        self.assertTrue(restore.should_play)
        self.assertEqual(seen, [("status", False)])


if __name__ == "__main__":
    unittest.main()