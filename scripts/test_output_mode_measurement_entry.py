#!/usr/bin/env python3
"""Focused Coordinator contracts for output-mode and measurement entry."""

from __future__ import annotations

import inspect
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main
import samplerate
from playback_transition import PlaybackTransitionCoordinator, PlaybackTransitionFailure, TransitionRequest


class TransactionRuntime:
    def __init__(
        self,
        *,
        fail_output_verify: bool = False,
        initially_muted: bool = False,
        initially_easyeffects_muted: bool = False,
        dsp_reinitialized: bool = False,
        real_reconcile: bool = False,
    ):
        self.events: list[str] = []
        self.muted = initially_muted
        self.easyeffects_muted = initially_easyeffects_muted
        self.fail_output_verify = fail_output_verify
        self.rate = 44100
        self.volume = 72
        self.paused = False
        self.playing = True
        self.spotify_status = "Playing"
        self.dsp_reinitialized = dsp_reinitialized
        self.real_reconcile = real_reconcile
        self.spotify_source_link_confirmed = False

    async def read_hardware_mute(self):
        self.events.append(f"read-mute:{self.muted}")
        return self.muted

    async def set_hardware_mute(self, muted, _transition_id):
        self.muted = bool(muted)
        self.events.append(f"mute:{self.muted}")

    async def read_sink_mute(self, sink_name):
        if sink_name != "easyeffects_sink":
            raise AssertionError(f"unexpected explicit sink: {sink_name}")
        self.events.append(f"read-sink-mute:{self.easyeffects_muted}")
        return self.easyeffects_muted

    async def set_sink_mute(self, sink_name, muted, _transition_id):
        if sink_name != "easyeffects_sink":
            raise AssertionError(f"unexpected explicit sink: {sink_name}")
        self.easyeffects_muted = bool(muted)
        self.events.append(f"sink-mute:{self.easyeffects_muted}")

    async def read_transition_snapshot(self, _request):
        self.events.append("snapshot")
        return {
            "player": {
                "current_file": "/music/current.flac",
                "playing": self.playing,
                "paused": self.paused,
                "volume": self.volume,
            },
            "output_mode_overview": {"output_mode": {"mode": "stereo"}},
            "output_mode_config": {"mode": "stereo"},
            "spotify": {"status": self.spotify_status},
        }

    async def quiet_old_source(self, _request):
        self.events.append("quiet")
        self.paused = True
        self.playing = False

    async def resolve_target_rate(self, request):
        self.events.append("resolve-rate")
        return request.target_rate

    async def establish_target_rate(self, request):
        self.events.append("target-rate")
        self.rate = request.target_rate

    async def establish_effects_and_helper(self, _request):
        self.events.append("effects-helper-links")
        return {"dsp_reinitialized": self.dsp_reinitialized}

    async def verify_measurement_entry(self, _request):
        self.events.append("verify-measurement-entry")
        return {"committed": True, "graph_complete": True}

    async def verify_output_mode_runtime(self, _request):
        self.events.append("verify-output-mode")
        if self.fail_output_verify:
            raise RuntimeError("target graph is incomplete")
        if not self.spotify_source_link_confirmed and _request.source == "spotify" and _request.should_play:
            raise RuntimeError("Spotify source link was not confirmed")
        return {"committed": True, "graph_complete": True}

    async def commit_output_mode_runtime(self, _request):
        self.events.append("persist-output-mode")
        return {"output_mode_persisted": True}

    async def rollback_output_mode_runtime(self, _request, _snapshot):
        self.events.append("rollback-output-mode")

    async def restore_output_mode_transport(self, _request, snapshot, _transition_id):
        self.events.append("restore-transport")
        if _request.source == "spotify":
            self.spotify_status = "Playing" if snapshot.get("spotify", {}).get("status") == "Playing" else "Paused"
            return
        previous = snapshot.get("player") or {}
        self.volume = int(previous.get("volume", self.volume))
        self.playing = bool(previous.get("playing") and not previous.get("paused"))
        self.paused = not self.playing

    async def set_source_volume(self, volume, _transition_id):
        self.events.append(f"source-volume:{volume}")
        self.volume = volume

    async def pause_source_after_failure(self, _request):
        self.events.append("pause-after-failure")
        self.paused = True
        self.playing = False

    async def abort_failed_transition(self, _request, _snapshot, *, target_staged):
        self.events.append(f"abort:{target_staged}")

    def target_source_staged(self, _request):
        return False

    async def verify_transition_graph(self, _request):
        self.events.append("verify-graph")
        return {"committed": True}

    async def verify_committed_transition(self, _request):
        self.events.append("commit-readback")
        return {"committed": True}

    async def prepare_target_source(self, _request):
        self.events.append("prepare")

    async def start_target_source(self, _request):
        self.events.append("start")

    async def reconcile_post_start_graph(self, _request):
        if self.real_reconcile:
            result = await main._coordinator_reconcile_post_start_graph(_request)
        else:
            result = {"graph_complete": True}
        if _request.source == "spotify" and _request.should_play:
            self.events.append("relink-spotify-source")
            self.spotify_source_link_confirmed = True
            self.events.append("post-start-source-link")
        else:
            self.events.append("post-start-graph")
        return result

    async def stabilize_effects_after_rate_change(self, _request, *, dsp_reinitialized=False):
        self.events.append("dsp-stabilize")
        return {"stabilized": True}


def _request(
    operation: str,
    *,
    target_rate: int = 44100,
    should_play: bool = True,
    source: str = "local",
) -> TransitionRequest:
    return TransitionRequest(
        operation=operation,
        source=source,
        target_rate=target_rate,
        target_url="/music/current.flac" if source != "spotify" else "spotify-track-1",
        target_track={"source": source, "url": "/music/current.flac" if source != "spotify" else "spotify-track-1"},
        should_play=should_play,
        rate_change=operation == "measurement-entry",
        reload_source=False,
        output_mode_target={"output_mode": {"mode": "subwoofer-2.1"}},
        output_mode_config={"mode": "subwoofer-2.1", "subwoofer": {}},
    )


class CoordinatorTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_spotify_play_clears_stale_hardware_and_internal_mutes(self):
        runtime = TransactionRuntime(
            initially_muted=True,
            initially_easyeffects_muted=True,
        )
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(_request("spotify-play", source="spotify"))

        self.assertTrue(result.committed)
        self.assertFalse(runtime.muted)
        self.assertFalse(runtime.easyeffects_muted)
        self.assertLess(runtime.events.index("mute:True"), runtime.events.index("sink-mute:False"))
        self.assertLess(runtime.events.index("sink-mute:False"), runtime.events.index("mute:False"))

    async def test_running_output_mode_switch_clears_stale_mutes(self):
        runtime = TransactionRuntime(
            initially_muted=True,
            initially_easyeffects_muted=True,
        )
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(_request("output-mode-switch"))

        self.assertTrue(result.committed)
        self.assertFalse(runtime.muted)
        self.assertFalse(runtime.easyeffects_muted)

    async def test_paused_output_mode_switch_preserves_existing_mutes(self):
        runtime = TransactionRuntime(
            initially_muted=True,
            initially_easyeffects_muted=True,
        )
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(_request("output-mode-switch", should_play=False))

        self.assertTrue(result.committed)
        self.assertTrue(runtime.muted)
        self.assertTrue(runtime.easyeffects_muted)

    async def test_measurement_entry_unmutes_both_sinks_before_sweep(self):
        runtime = TransactionRuntime(
            initially_muted=True,
            initially_easyeffects_muted=True,
        )
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(
            _request("measurement-entry", target_rate=48000, should_play=False)
        )

        self.assertTrue(result.committed)
        self.assertFalse(runtime.muted)
        self.assertFalse(runtime.easyeffects_muted)
        self.assertLess(
            runtime.events.index("sink-mute:False"),
            runtime.events.index("verify-measurement-entry"),
        )

    async def test_output_mode_persists_only_after_stable_graph_and_restores_transport(self):
        runtime = TransactionRuntime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(_request("output-mode-switch"))

        self.assertTrue(result.committed)
        self.assertLess(runtime.events.index("mute:True"), runtime.events.index("restore-transport"))
        self.assertLess(runtime.events.index("restore-transport"), runtime.events.index("post-start-graph"))
        self.assertLess(runtime.events.index("post-start-graph"), runtime.events.index("verify-output-mode"))
        self.assertLess(runtime.events.index("verify-output-mode"), runtime.events.index("persist-output-mode"))
        self.assertLess(runtime.events.index("persist-output-mode"), runtime.events.index("mute:False"))
        self.assertTrue(runtime.playing)
        self.assertEqual(runtime.volume, 72)
        self.assertFalse(coordinator.gate.closed)

    async def test_output_mode_failure_rolls_back_and_latches_gate_without_persisting_target(self):
        runtime = TransactionRuntime(fail_output_verify=True)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        with self.assertRaises(PlaybackTransitionFailure):
            await coordinator.execute(_request("output-mode-switch"))

        self.assertNotIn("persist-output-mode", runtime.events)
        self.assertIn("rollback-output-mode", runtime.events)
        self.assertTrue(coordinator.gate.failure_latched)
        self.assertTrue(coordinator.gate.closed)
        self.assertTrue(runtime.muted)

    async def test_measurement_entry_uses_coordinator_rate_and_graph_before_sweep(self):
        runtime = TransactionRuntime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(_request("measurement-entry", target_rate=48000, should_play=False))

        self.assertTrue(result.committed)
        self.assertEqual(runtime.rate, 48000)
        self.assertTrue(runtime.paused)
        self.assertNotIn("prepare", runtime.events)
        self.assertNotIn("start", runtime.events)
        self.assertLess(runtime.events.index("target-rate"), runtime.events.index("verify-measurement-entry"))
        self.assertLess(runtime.events.index("verify-measurement-entry"), runtime.events.index("mute:False"))

    async def test_spotify_mode_switch_relinks_new_source_before_gate_reopens(self):
        runtime = TransactionRuntime(real_reconcile=True)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        initial = {
            "mode": "stereo",
            "output_key": "alsa_output.test",
            "ee_ports": True,
            "helper_ports": None,
            "helper_active": None,
            "helper_rate": None,
            "helper_rate_matches": None,
            "source_links": {
                "spotify:output_FL -> easyeffects_sink:playback_FL": False,
                "spotify:output_FR -> easyeffects_sink:playback_FR": False,
            },
            "source_links_complete": False,
            "direct_ee_to_hw_present": False,
            "links": {
                "ee_soe_output_level:output_FL -> alsa_output.test:playback_FL": True,
                "ee_soe_output_level:output_FR -> alsa_output.test:playback_FR": True,
            },
            "links_complete": False,
            "port_identities": {
                "source": ("spotify:output_FL", "spotify:output_FR"),
                "source_target": (
                    "easyeffects_sink:playback_FL",
                    "easyeffects_sink:playback_FR",
                ),
                "ee": ("ee_soe_output_level:output_FL", "ee_soe_output_level:output_FR"),
                "helper": (),
                "output": (
                    "alsa_output.test:playback_FL",
                    "alsa_output.test:playback_FR",
                ),
            },
            "signature": "spotify-source-missing",
        }
        stable = dict(initial)
        stable["source_links"] = {
            key: True for key in initial["source_links"]
        }
        stable["source_links_complete"] = True
        stable["links_complete"] = True
        stable["signature"] = "spotify-source-stable"
        with patch.object(main, "_playback_graph_diagnosis", new=AsyncMock(side_effect=[initial, stable, stable])) as diagnosis, patch.object(
            main, "_connect_ports", new=AsyncMock()
        ) as relink:
            result = await coordinator.execute(_request("output-mode-switch", source="spotify"))

        self.assertTrue(result.committed)
        self.assertEqual(diagnosis.await_count, 3)
        self.assertEqual(
            relink.await_args_list,
            [
                call(("spotify:output_FL",), "easyeffects_sink:playback_FL"),
                call(("spotify:output_FR",), "easyeffects_sink:playback_FR"),
            ],
        )
        self.assertLess(runtime.events.index("restore-transport"), runtime.events.index("relink-spotify-source"))
        self.assertLess(runtime.events.index("relink-spotify-source"), runtime.events.index("verify-output-mode"))
        self.assertLess(runtime.events.index("post-start-source-link"), runtime.events.index("persist-output-mode"))
        self.assertLess(runtime.events.index("persist-output-mode"), runtime.events.index("mute:False"))
        self.assertTrue(runtime.spotify_source_link_confirmed)

    async def test_output_mode_dsp_reinitialization_stabilizes_before_gate_reopens(self):
        runtime = TransactionRuntime(dsp_reinitialized=True)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(_request("output-mode-switch"))

        self.assertTrue(result.committed)
        self.assertLess(runtime.events.index("verify-output-mode"), runtime.events.index("dsp-stabilize"))
        self.assertLess(runtime.events.index("dsp-stabilize"), runtime.events.index("persist-output-mode"))
        self.assertLess(runtime.events.index("persist-output-mode"), runtime.events.index("mute:False"))


class EntryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_mode_endpoint_submits_target_to_coordinator(self):
        class Request:
            async def json(self):
                return {"mode": "subwoofer-2.1", "subwoofer": {}}

        target = {
            "overview": {"output_mode": {"mode": "subwoofer-2.1"}},
            "config": {"mode": "subwoofer-2.1", "subwoofer": {}},
        }
        run = AsyncMock(return_value=SimpleNamespace(committed=True))
        with patch.object(main, "measurement_sr_session", SimpleNamespace(active=False)), patch.object(
            main, "prepare_audio_output_mode", return_value=target
        ), patch.object(main, "_coordinator_current_playback_context", new=AsyncMock(return_value={
            "source": "local",
            "target_url": "/music/current.flac",
            "target_track": {"source": "local", "url": "/music/current.flac"},
            "should_play": True,
        })), patch.object(main, "get_samplerate_status", return_value={"active_rate": 44100}), patch.object(
            main, "_run_coordinated_transition", run
        ), patch.object(main, "get_audio_output_overview", return_value=target["overview"]), patch.object(
            main, "_with_subwoofer_derived_delays", side_effect=lambda value: value
        ), patch.object(main, "subwoofer_runtime", None), patch.object(
            main, "refresh_peak_monitor_after_effects_change", new=AsyncMock()
        ):
            await main.save_audio_output_mode_route(Request())

        run.assert_awaited_once()
        request = run.await_args.args[0]
        self.assertEqual(request.operation, "output-mode-switch")
        self.assertEqual(request.output_mode_target, target["overview"])
        self.assertEqual(request.output_mode_config, target["config"])

    async def test_measurement_session_entry_submits_rate_change_to_coordinator(self):
        session = main.MeasurementSampleRateSession()
        result = SimpleNamespace(committed=True, target_rate=48000)
        run = AsyncMock(return_value=result)
        originals = {
            "measurement_sr_session": main.measurement_sr_session,
            "current_track_info": main.current_track_info,
            "player_instance": main.player_instance,
        }
        try:
            main.measurement_sr_session = session
            main.current_track_info = None
            main.player_instance = None
            with patch.object(main, "_capture_playback_state_before_measurement"), patch.object(
                main, "get_samplerate_status", return_value={"force_rate": 44100, "active_rate": 44100}
            ), patch.object(main, "_coordinator_current_playback_context", new=AsyncMock(return_value={
                "source": "local",
                "target_url": None,
                "target_track": {},
                "should_play": False,
            })), patch.object(main, "_run_coordinated_transition", run):
                await session._start_locked(48000)
        finally:
            for name, value in originals.items():
                setattr(main, name, value)

        request = run.await_args.args[0]
        self.assertEqual(request.operation, "measurement-entry")
        self.assertEqual(request.target_rate, 48000)
        self.assertTrue(request.rate_change)

    async def test_measurement_preflight_requires_route_ports_and_complete_graph(self):
        class Store:
            def _resolve_playback_target(self):
                return {"target_name": "alsa_output.test"}

            def _build_measurement_playback_route(self, _node, target):
                return {
                    "route": "direct-sink",
                    "playback_target_name": target["target_name"],
                }

            def _list_pw_ports(self, target):
                return [f"{target}:playback_FL", f"{target}:playback_FR"]

        coordinator = SimpleNamespace(
            transition_active=False,
            gate=SimpleNamespace(failure_latched=False, closed=False),
        )
        with patch.object(main, "playback_transition_coordinator", coordinator), patch.object(
            main, "measurement_store", Store()
        ), patch.object(main, "get_samplerate_status", return_value={
            "active_rate": 48000,
            "force_rate": 48000,
        }), patch.object(main, "_playback_graph_diagnosis", new=AsyncMock(return_value={
            "links_complete": True,
            "signature": "stable",
        })):
            await main._measurement_entry_preflight(48000)

    async def test_measurement_entry_and_output_mode_endpoint_sources_have_no_direct_graph_mutators(self):
        output_mode_source = inspect.getsource(main.save_audio_output_mode_route)
        start_source = inspect.getsource(main.MeasurementSampleRateSession._start_locked)
        self.assertIn("_run_coordinated_transition", output_mode_source)
        self.assertIn("prepare_audio_output_mode", output_mode_source)
        self.assertNotIn("set_audio_output_mode(", output_mode_source)
        self.assertNotIn("_sync_subwoofer_runtime", output_mode_source)
        self.assertNotIn("load_preset", output_mode_source)
        self.assertIn("operation=\"measurement-entry\"", start_source)
        self.assertIn("_run_coordinated_transition", start_source)
        self.assertNotIn("_set_pipewire_force_rate", start_source)
        self.assertNotIn("_sync_subwoofer_runtime", start_source)

    def test_default_sample_rate_ui_and_frontend_post_are_removed(self):
        index = (pathlib.Path(__file__).resolve().parents[1] / "static" / "index.html").read_text()
        app = (pathlib.Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
        samplerate_source = (pathlib.Path(__file__).resolve().parents[1] / "samplerate.py").read_text()
        self.assertNotIn("settings-samplerate-select", index)
        self.assertNotIn("Default sample rate", index)
        self.assertNotIn("savePipewireDefaultRate", app)
        self.assertNotIn("set_pipewire_default_rate_selection", samplerate_source)
        self.assertNotIn("_render_pipewire_clock_rate_dropin", samplerate_source)
        self.assertNotIn("method: 'POST'", app[app.find("/api/audio/samplerate") - 100: app.find("/api/audio/samplerate") + 150])
        self.assertIn("/api/audio/samplerate", app)


class OutputModePersistenceSplitTests(unittest.TestCase):
    def test_prepare_does_not_persist_and_commit_persists_validated_config(self):
        with tempfile.TemporaryDirectory(prefix="fxroute-output-mode-test-") as directory:
            path = pathlib.Path(directory) / "audio-output-mode.json"
            overview = {
                "output_mode": {
                    "mode": "stereo",
                    "available": True,
                }
            }
            with patch.object(samplerate, "_audio_output_mode_path", return_value=path), patch.object(
                samplerate, "get_audio_output_overview", return_value=overview
            ):
                target = samplerate.prepare_audio_output_mode("stereo")
                self.assertFalse(path.exists())
                samplerate.persist_audio_output_mode(target["config"])
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text() and target["config"]["mode"], "stereo")


if __name__ == "__main__":
    unittest.main()
