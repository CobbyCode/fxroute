#!/usr/bin/env python3
"""Second responsiveness pass: hot-path offload and redundant-read regressions.

Pins the follow-up fixes to commit 6b2e8df:

- ``_wait_for_sink_input_release`` must poll its pactl listing off the event
  loop (the release waits run inside every rate-changing transition).
- ``get_audio_output_overview(status=...)`` must reuse a caller-provided
  samplerate status instead of rebuilding that pipeline per snapshot.
- ``get_samplerate_status`` keeps identical parsing/notes semantics while its
  four independent command reads run concurrently.
"""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
import audio.samplerate.overview as overview_module

MAIN_THREAD = threading.current_thread()

WPCTL_INSPECT = (
    "id 57,\n"
    '    * node.name = "alsa_output.usb"\n'
    '    * node.description = "USB Headphone"\n'
)
PACTL_SINKS_SHORT = (
    "79\talsa_output.usb\tPipeWire\ts16le 2ch 44100Hz\tRUNNING\n"
    "55\tfxroute_dsp_sink\tPipeWire\tfloat32le 2ch 44100Hz\tRUNNING\n"
)
PW_METADATA = (
    "key:'clock.rate' value:'44100'\n"
    "key:'clock.force-rate' value:'0'\n"
    "key:'clock.allowed-rates' value:'[44100 48000]'\n"
)
PW_CORE_INFO = 'default.clock.rate = "44100"\n'
PW_NODES = (
    "id 57,\n"
    '    node.name = "alsa_output.usb"\n'
    "id 55,\n"
    '    node.name = "fxroute_dsp_sink"\n'
)
PW_ENUM_FORMAT = (
    "Audio:rate (Standard)\n"
    "    Int 44100\n"
)


def _stub_run_command(delay_s: float = 0.0):
    """Return a _run_command stand-in serving canned outputs per command."""
    commands = []

    def run(args):
        commands.append(tuple(args))
        if delay_s:
            time.sleep(delay_s)
        if args[:2] == ["pw-metadata", "-n"]:
            return PW_METADATA
        if args[:2] == ["wpctl", "inspect"]:
            return WPCTL_INSPECT
        if args[:2] == ["pactl", "list"] and args[-1] == "short":
            return PACTL_SINKS_SHORT
        if args[:2] == ["pactl", "list"]:
            return ""
        if args[:2] == ["pw-cli", "info"]:
            return PW_CORE_INFO
        if args[:2] == ["pw-cli", "ls"]:
            return PW_NODES
        if args[:2] == ["pw-cli", "enum-params"]:
            return PW_ENUM_FORMAT
        return ""

    run.commands = commands
    return run


class WaitForSinkInputReleaseOffLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_release_wait_polls_listing_off_loop(self):
        threads = []

        def probe():
            threads.append(threading.current_thread() is MAIN_THREAD)
            return len(threads) < 2  # busy once, released on second poll

        released = await main._wait_for_sink_input_release(probe, timeout_ms=2000)
        self.assertTrue(released)
        self.assertEqual(len(threads), 2)
        self.assertFalse(
            any(threads),
            "release wait ran the pactl sink-input listing on the event loop",
        )


class OverviewStatusReuseTest(unittest.TestCase):
    def test_overview_reuses_injected_status_without_rebuild(self):
        stub = _stub_run_command()
        counter = {"status_builds": 0}

        def counted_status():
            counter["status_builds"] += 1
            return {
                "available": True,
                "notes": [],
                "sink": {"id": 57, "name": "alsa_output.usb", "description": "USB"},
                "relevant_sink": {"name": "alsa_output.usb", "active_rate": 44100},
                "force_rate": 0,
                "policy": {"mode": "auto"},
            }

        with patch.object(
            overview_module, "_run_command", stub
        ), patch.object(
            overview_module, "get_bluetooth_audio_overview",
            lambda: {"available": False},
        ), patch.object(
            overview_module, "get_samplerate_status", counted_status
        ):
            injected = counted_status()
            overview = overview_module.get_audio_output_overview(status=injected)
            self.assertEqual(counter["status_builds"], 1, "injected status was rebuilt")
            keys = [output["key"] for output in overview["outputs"]]
            self.assertIn("fxroute_dsp_sink", keys)
            dsp_output = next(o for o in overview["outputs"] if o["key"] == "fxroute_dsp_sink")
            self.assertIn(44100, dsp_output["native_supported_rates"])
            self.assertEqual(
                overview["output_mode"]["effective_output_key"], "alsa_output.usb"
            )

            # Without an injected status the builder fetches one itself.
            overview_module.get_audio_output_overview()
            self.assertEqual(counter["status_builds"], 2)


class SamplerateStatusConcurrencyTest(unittest.TestCase):
    def test_concurrent_reads_keep_parse_semantics(self):
        stub = _stub_run_command(delay_s=0.04)

        with patch.object(overview_module, "_run_command", stub):
            start = time.monotonic()
            status = overview_module.get_samplerate_status()
            elapsed = time.monotonic() - start

        # Four ~40 ms reads: concurrent execution must stay well under the
        # serial sum (~160 ms) while every read still happens exactly once.
        self.assertLess(elapsed, 0.14, f"status reads did not overlap: {elapsed:.3f}s")
        issued = stub.commands
        for expected in (
            ("pw-metadata", "-n"),
            ("wpctl", "inspect"),
            ("pactl", "list"),
            ("pw-cli", "info"),
        ):
            self.assertIn(expected, [tuple(cmd[:2]) for cmd in issued])

        self.assertTrue(status["available"])
        self.assertEqual(status["sink"]["name"], "alsa_output.usb")
        self.assertEqual(status["relevant_sink"]["name"], "alsa_output.usb")
        self.assertEqual(status["active_rate"], 44100)
        self.assertEqual(status["clock_rate"], 44100)
        self.assertEqual(status["force_rate"], 0)
        self.assertEqual(status["default_rate"], 44100)
        self.assertEqual(status["allowed_rates"], [44100, 48000])
        self.assertEqual(status["notes"], [])


class GraphDiagnosisParallelReadsTest(unittest.IsolatedAsyncioTestCase):
    """playback_graph_diagnosis issues its two pw-link reads concurrently."""

    async def test_pw_link_reads_overlap_and_verdict_is_unchanged(self):
        import asyncio
        from types import SimpleNamespace

        import playback.orchestration as orchestration_module

        calls = []

        async def fake_pw_link(*args: str) -> str:
            calls.append(tuple(args))
            await asyncio.sleep(0.05)
            if args == ("-io",):
                return (
                    "mpv:output_FL\n"
                    "fxroute_dsp_sink:playback_FL\n"
                    "fxroute_dsp_sink:playback_FR\n"
                    "fxroute_dsp_sink:monitor_FL\n"
                    "fxroute_dsp_sink:monitor_FR\n"
                    "fxroute_dsp:input_1\n"
                    "fxroute_dsp:input_2\n"
                    "fxroute_dsp:output_1\n"
                    "fxroute_dsp:output_2\n"
                )
            return (
                "fxroute_dsp_sink:monitor_FL -> fxroute_dsp:input_1\n"
                "fxroute_dsp_sink:monitor_FR -> fxroute_dsp:input_2\n"
                "fxroute_dsp:output_1 -> alsa_output.test:playback_FL\n"
                "fxroute_dsp:output_2 -> alsa_output.test:playback_FR\n"
                "mpv:output_FL -> fxroute_dsp_sink:playback_FL\n"
                "mpv:output_FR -> fxroute_dsp_sink:playback_FR\n"
            )

        deps = SimpleNamespace(
            run_pw_link_command=fake_pw_link,
            output_mode_subwoofer_modes=frozenset({"subwoofer-2.1", "subwoofer-2.2"}),
            output_mode_stereo="stereo",
            get_dsp_snapshot=lambda: {"active": True},
            helper_argument_sample_rate=lambda snapshot: 44100,
            resolve_source_producer_ports=None,
            contains_link=None,
        )
        # contains_link is a pure function on text; bind the real one.
        from playback.orchestration import PlaybackOrchestrator  # noqa: F401
        from dsp.runtime import _contains_link as real_contains_link

        deps.contains_link = real_contains_link

        orchestrator = type(
            "_Orchestrator",
            (),
            {
                "_deps": deps,
                "playback_graph_diagnosis": orchestration_module.PlaybackOrchestrator.playback_graph_diagnosis,
            },
        )()
        overview = {
            "output_mode": {
                "mode": "stereo",
                "effective_output_key": "alsa_output.test",
            }
        }
        start = time.monotonic()
        diagnosis = await orchestrator.playback_graph_diagnosis(
            overview, source="radio", target_rate=44100, require_source=True
        )
        elapsed = time.monotonic() - start

        self.assertEqual(calls, [("-io",), ("-l",)])
        # Serial execution would have cost ~2x the single-read sleep.
        self.assertLess(elapsed, 0.095, f"pw-link reads did not overlap: {elapsed:.3f}s")
        self.assertTrue(diagnosis["links_complete"])
        self.assertFalse(diagnosis["bypass_only"])


class GateSinkEpisodeMemoTest(unittest.IsolatedAsyncioTestCase):
    """The gate sink resolves once per episode and re-resolves on invalidation."""

    async def test_read_reuses_resolution_until_invalidated(self):
        from playback.runtime.mute import _RuntimeMuteMixin

        resolutions = {"count": 0}

        class _Deps:
            @staticmethod
            def get_samplerate_status():
                resolutions["count"] += 1
                return {
                    "relevant_sink": {"name": "alsa_output.test"},
                    "active_rate": 44100,
                    "force_rate": 0,
                }

            @staticmethod
            def get_audio_output_overview():
                return {"output_mode": {}}

        class _Adapter(_RuntimeMuteMixin):
            def __init__(self):
                self._deps = _Deps()

        with patch(
            "playback.runtime.mute._read_hardware_sink_mute",
            lambda output_key: False,
        ):
            adapter = _Adapter()
            await adapter.read_hardware_mute()
            await adapter.read_hardware_mute()
            self.assertEqual(resolutions["count"], 1, "gate sink re-resolved within episode")

            adapter.invalidate_gate_sink_resolution()
            await adapter.read_hardware_mute()
            self.assertEqual(resolutions["count"], 2, "invalidation did not force re-resolution")


if __name__ == "__main__":
    unittest.main()
