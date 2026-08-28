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
import playback.orchestration as playback_orchestration
import audio.pw_link as pw_link_mod
import audio.samplerate as samplerate
import playback.orchestration as playback_orchestration
from playback_transition_test_support import make_transition_runtime
import measurement.session as measurement_session
import audio.samplerate as samplerate
from playback.transition import PlaybackTransitionCoordinator, PlaybackTransitionFailure, TransitionRequest


class TransactionRuntime:
    def __init__(
        self,
        *,
        fail_output_verify: bool = False,
        initially_muted: bool = False,
        initially_dsp_muted: bool = False,
        dsp_reinitialized: bool = False,
        real_reconcile: bool = False,
        fail_gate_restore: bool = False,
    ):
        self.events: list[str] = []
        self.muted = initially_muted
        self.dsp_muted = initially_dsp_muted
        self.fail_output_verify = fail_output_verify
        self.rate = 44100
        self.helper_rate = 44100
        self.force_rate = 44100
        self.volume = 72
        self.position = 123.5
        self.paused = False
        self.playing = True
        self.spotify_status = "Playing"
        self.sample_rate_policy = {"mode": "auto", "rate": None}
        self.dsp_reinitialized = dsp_reinitialized
        self.real_reconcile = real_reconcile
        self.fail_gate_restore = fail_gate_restore
        self.spotify_source_link_confirmed = False

    async def read_hardware_mute(self):
        self.events.append(f"read-mute:{self.muted}")
        return self.muted

    async def set_hardware_mute(self, muted, _transition_id):
        if not muted and self.fail_gate_restore:
            raise RuntimeError("hardware gate did not reopen")
        self.muted = bool(muted)
        self.events.append(f"mute:{self.muted}")

    async def read_sink_mute(self, sink_name):
        if sink_name != "fxroute_dsp_sink":
            raise AssertionError(f"unexpected explicit sink: {sink_name}")
        self.events.append(f"read-sink-mute:{self.dsp_muted}")
        return self.dsp_muted

    async def set_sink_mute(self, sink_name, muted, _transition_id):
        if sink_name != "fxroute_dsp_sink":
            raise AssertionError(f"unexpected explicit sink: {sink_name}")
        self.dsp_muted = bool(muted)
        self.events.append(f"sink-mute:{self.dsp_muted}")

    async def read_transition_snapshot(self, _request):
        self.events.append("snapshot")
        return {
            "player": {
                "current_file": "/music/current.flac",
                "playing": self.playing,
                "paused": self.paused,
                "volume": self.volume,
                "position": self.position,
            },
            "output_mode_overview": {"output_mode": {"mode": "stereo"}},
            "output_mode_config": {"mode": "stereo"},
            "spotify": {"status": self.spotify_status},
            "sample_rate_policy": dict(self.sample_rate_policy),
            "active_rate": self.rate,
            "force_rate": self.force_rate,
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
        if _request.rate_change:
            self.helper_rate = _request.target_rate
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

    async def commit_sample_rate_policy(self, _request):
        self.events.append("persist-sample-rate-policy")
        self.sample_rate_policy = dict(_request.sample_rate_policy)
        return {"sample_rate_policy": dict(_request.sample_rate_policy)}

    async def rollback_sample_rate_policy(self, _request, snapshot):
        self.events.append("rollback-sample-rate-policy")
        self.sample_rate_policy = dict(snapshot["sample_rate_policy"])

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
        if _request.restore_position is not None:
            self.position = float(_request.restore_position)
            self.events.append(f"seek:{self.position}")

    async def start_target_source(self, _request):
        self.events.append("start")
        self.playing = bool(_request.should_play)
        self.paused = not self.playing

    async def reconcile_post_start_graph(self, _request):
        if self.real_reconcile:
            result = await playback_orchestration.configured().reconcile_post_start_graph(_request)
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
    async def test_sample_rate_policy_real_local_changes_use_full_handoff(self):
        for initial_rate, target_rate in ((44100, 48000), (48000, 44100)):
            with self.subTest(initial_rate=initial_rate, target_rate=target_rate):
                runtime = TransactionRuntime()
                runtime.rate = initial_rate
                runtime.helper_rate = initial_rate
                coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
                request = TransitionRequest(
                    operation="sample-rate-policy",
                    source="local",
                    target_rate=target_rate,
                    target_url="/music/current.flac",
                    target_track={"source": "local", "url": "/music/current.flac"},
                    should_play=True,
                    rate_change=True,
                    reload_source=True,
                    sample_rate_policy={"mode": "fixed", "rate": target_rate},
                )

                result = await coordinator.execute(request)

                self.assertTrue(result.committed)
                self.assertEqual(runtime.rate, target_rate)
                self.assertEqual(runtime.helper_rate, target_rate)
                self.assertTrue(runtime.playing)
                self.assertFalse(runtime.paused)
                self.assertFalse(runtime.muted)
                self.assertEqual(runtime.position, 123.5)
                expected = [
                    "quiet", "target-rate", "effects-helper-links", "prepare",
                    "seek:123.5", "start", "verify-graph", "commit-readback",
                    "persist-sample-rate-policy",
                ]
                indices = [runtime.events.index(event) for event in expected]
                self.assertEqual(indices, sorted(indices))

    async def test_sample_rate_policy_same_rate_keeps_non_reload_path(self):
        runtime = TransactionRuntime()
        runtime.rate = 48000
        runtime.helper_rate = 48000
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        request = TransitionRequest(
            operation="sample-rate-policy",
            source="local",
            target_rate=48000,
            target_url="/music/current.flac",
            target_track={"source": "local", "url": "/music/current.flac"},
            should_play=True,
            rate_change=False,
            reload_source=False,
            sample_rate_policy={"mode": "fixed", "rate": 48000},
        )

        result = await coordinator.execute(request)

        self.assertTrue(result.committed)
        self.assertNotIn("prepare", runtime.events)
        self.assertNotIn("start", runtime.events)
        self.assertNotIn("seek:123.5", runtime.events)
        self.assertEqual(runtime.helper_rate, 48000)
        self.assertIn("persist-sample-rate-policy", runtime.events)

    async def test_spotify_sample_rate_policy_uses_full_replay_path(self):
        runtime = TransactionRuntime()
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        request = TransitionRequest(
            operation="sample-rate-policy",
            source="spotify",
            target_rate=48000,
            target_url="spotify-track-1",
            should_play=True,
            rate_change=True,
            reload_source=True,
            sample_rate_policy={"mode": "fixed", "rate": 48000},
        )

        result = await coordinator.execute(request)

        self.assertTrue(result.committed)
        expected = [
            "quiet", "target-rate", "effects-helper-links", "prepare", "start",
            "post-start-source-link", "verify-graph", "dsp-stabilize",
            "commit-readback", "persist-sample-rate-policy",
        ]
        indices = [runtime.events.index(event) for event in expected]
        self.assertEqual(indices, sorted(indices))
        self.assertNotIn("restore-transport", runtime.events)

    async def test_sample_rate_policy_rolls_back_when_gate_restore_fails(self):
        runtime = TransactionRuntime(fail_gate_restore=True)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)
        request = TransitionRequest(
            operation="sample-rate-policy",
            source="local",
            target_rate=44100,
            target_url="/music/current.flac",
            target_track={"source": "local", "url": "/music/current.flac"},
            should_play=True,
            rate_change=False,
            reload_source=False,
            sample_rate_policy={"mode": "fixed", "rate": 44100},
        )

        with self.assertRaises(PlaybackTransitionFailure):
            await coordinator.execute(request)

        self.assertEqual(runtime.sample_rate_policy, {"mode": "auto", "rate": None})
        self.assertLess(
            runtime.events.index("persist-sample-rate-policy"),
            runtime.events.index("rollback-sample-rate-policy"),
        )

    async def test_sample_rate_policy_rollback_restores_live_force_rate(self):
        # The transition re-pinned the hardware to the failed target rate;
        # the production rollback must also restore the pre-transition live
        # pin of the restored fixed policy, not only the JSON policy file.
        runtime = make_transition_runtime()
        snapshot = {
            "sample_rate_policy": {"mode": "fixed", "rate": 44100},
            "active_rate": 44100,
            "force_rate": 44100,
        }
        restored_pins = []

        def restore_pin(rate):
            restored_pins.append(rate)

        with patch.object(
            main.samplerate, "set_pipewire_force_rate", new=restore_pin
        ), patch.object(
            main.samplerate, "persist_sample_rate_policy",
            return_value={"mode": "fixed", "rate": 44100},
        ) as persist:
            await runtime.rollback_sample_rate_policy(None, snapshot)

        persist.assert_called_once_with({"mode": "fixed", "rate": 44100})
        self.assertEqual(restored_pins, [44100])

    async def test_sample_rate_policy_rollback_clears_pin_for_auto_policy(self):
        runtime = make_transition_runtime()
        snapshot = {
            "sample_rate_policy": {"mode": "auto", "rate": None},
            "active_rate": 44100,
            "force_rate": 48000,
        }
        cleared = []

        def clear_pin(expected_rate, **kwargs):
            cleared.append(expected_rate)
            return True

        with patch.object(
            main.samplerate, "clear_auto_policy_force_rate", new=clear_pin
        ):
            await runtime.rollback_sample_rate_policy(None, snapshot)

        self.assertEqual(cleared, [44100])

    async def test_spotify_play_clears_stale_hardware_and_internal_mutes(self):
        runtime = TransactionRuntime(
            initially_muted=True,
            initially_dsp_muted=True,
        )
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(_request("spotify-play", source="spotify"))

        self.assertTrue(result.committed)
        self.assertFalse(runtime.muted)
        self.assertFalse(runtime.dsp_muted)
        self.assertLess(runtime.events.index("mute:True"), runtime.events.index("sink-mute:False"))
        self.assertLess(runtime.events.index("sink-mute:False"), runtime.events.index("mute:False"))

    async def test_running_output_mode_switch_clears_stale_mutes(self):
        runtime = TransactionRuntime(
            initially_muted=True,
            initially_dsp_muted=True,
        )
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(_request("output-mode-switch"))

        self.assertTrue(result.committed)
        self.assertFalse(runtime.muted)
        self.assertFalse(runtime.dsp_muted)

    async def test_paused_output_mode_switch_preserves_existing_mutes(self):
        runtime = TransactionRuntime(
            initially_muted=True,
            initially_dsp_muted=True,
        )
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(_request("output-mode-switch", should_play=False))

        self.assertTrue(result.committed)
        self.assertTrue(runtime.muted)
        self.assertTrue(runtime.dsp_muted)

    async def test_measurement_entry_unmutes_both_sinks_before_sweep(self):
        runtime = TransactionRuntime(
            initially_muted=True,
            initially_dsp_muted=True,
        )
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(
            _request("measurement-entry", target_rate=48000, should_play=False)
        )

        self.assertTrue(result.committed)
        self.assertFalse(runtime.muted)
        self.assertFalse(runtime.dsp_muted)
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
            "dsp_ports": True,
            "helper_ports": True,
            "helper_active": True,
            "helper_rate": 48000,
            "helper_rate_matches": True,
            "source_links": {
                "spotify:output_FL -> fxroute_dsp_sink:playback_FL": False,
                "spotify:output_FR -> fxroute_dsp_sink:playback_FR": False,
            },
            "source_links_complete": False,
            "links": {
                "fxroute_dsp:output_FL -> alsa_output.test:playback_FL": True,
                "fxroute_dsp:output_FR -> alsa_output.test:playback_FR": True,
            },
            "links_complete": False,
            "port_identities": {
                "source": ("spotify:output_FL", "spotify:output_FR"),
                "source_target": (
                    "fxroute_dsp_sink:playback_FL",
                    "fxroute_dsp_sink:playback_FR",
                ),
                "dsp": ("fxroute_dsp:output_FL", "fxroute_dsp:output_FR"),
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
        with patch.object(playback_orchestration.configured(), "playback_graph_diagnosis", new=AsyncMock(side_effect=[initial, stable, stable])) as diagnosis, patch.object(
            pw_link_mod, "connect_ports", new=AsyncMock()
        ) as relink:
            result = await coordinator.execute(_request("output-mode-switch", source="spotify"))

        self.assertTrue(result.committed)
        self.assertEqual(diagnosis.await_count, 3)
        self.assertEqual(
            relink.await_args_list,
            [
                call(("spotify:output_FL",), "fxroute_dsp_sink:playback_FL"),
                call(("spotify:output_FR",), "fxroute_dsp_sink:playback_FR"),
            ],
        )
        self.assertLess(runtime.events.index("restore-transport"), runtime.events.index("relink-spotify-source"))
        self.assertLess(runtime.events.index("relink-spotify-source"), runtime.events.index("verify-output-mode"))
        self.assertLess(runtime.events.index("post-start-source-link"), runtime.events.index("persist-output-mode"))
        self.assertLess(runtime.events.index("persist-output-mode"), runtime.events.index("mute:False"))
        self.assertTrue(runtime.spotify_source_link_confirmed)

    async def test_subwoofer_switch_final_readback_settles_over_transient_missing_link(self):
        """A subwoofer output-mode switch must not fail when the final
        diagnosis transiently misses a single helper input edge that the
        runtime repair had just confirmed present."""
        overview = {
            "output_mode": {
                "mode": "subwoofer-2.2",
                "effective_output_key": "alsa_output.test",
            }
        }
        incomplete = {
            "mode": "subwoofer-2.2",
            "output_key": "alsa_output.test",
            "dsp_ports": True,
            "helper_ports": True,
            "helper_active": True,
            "helper_rate": 48000,
            "helper_rate_matches": True,
            "source_links": {},
            "source_links_complete": None,
            "links": {
                "fxroute_dsp_sink:monitor_FL -> fxroute_dsp:input_1": False,
                "fxroute_dsp_sink:monitor_FR -> fxroute_dsp:input_2": True,
                "fxroute_dsp:output_1 -> alsa_output.test:playback_FL": True,
                "fxroute_dsp:output_2 -> alsa_output.test:playback_FR": True,
                "fxroute_dsp:output_3 -> alsa_output.test:playback_RL": True,
                "fxroute_dsp:output_4 -> alsa_output.test:playback_RR": True,
            },
            "links_complete": False,
            "bypass_only": False,
            "port_identities": {
                "source": (),
                "source_target": (),
                "dsp": ("fxroute_dsp_sink:monitor_FL", "fxroute_dsp_sink:monitor_FR"),
                "helper": (
                    "fxroute_dsp:input_1", "fxroute_dsp:input_2",
                    "fxroute_dsp:output_1", "fxroute_dsp:output_2",
                    "fxroute_dsp:output_3", "fxroute_dsp:output_4",
                ),
                "output": ("alsa_output.test:playback_FL", "alsa_output.test:playback_FR"),
            },
            "signature": "subwoofer-input-l-missing",
        }
        complete = dict(incomplete)
        complete["links"] = {key: True for key in incomplete["links"]}
        complete["links_complete"] = True
        complete["signature"] = "subwoofer-links-stable"
        request = TransitionRequest(
            operation="output-mode-switch",
            source="local",
            target_rate=48000,
            target_url="/music/current.flac",
            target_track={"source": "local", "url": "/music/current.flac"},
            should_play=True,
            rate_change=False,
            reload_source=False,
            output_mode_target=overview,
            output_mode_config={"mode": "subwoofer-2.2"},
        )
        with patch.object(main.runtime, "dsp_runtime", SimpleNamespace()), patch.object(
            main, "dsp_manager", None
        ), patch.object(
            playback_orchestration.configured(), "playback_graph_diagnosis", new=AsyncMock(side_effect=[incomplete, incomplete, complete])
        ), patch.object(main.dsp_orchestrator, "sync_runtime", new=AsyncMock()        ), patch.object(playback_orchestration.configured(), "wait_for_dsp_output_ports", new=AsyncMock(return_value=True)), patch.object(
            samplerate, "reconcile_transition_sink_rate", new=AsyncMock(return_value=True)
        ), patch.object(
            playback_orchestration.configured(), "reconcile_subwoofer_links_only", new=AsyncMock()
        ), patch.object(pw_link_mod, "connect_ports", new=AsyncMock()
        ), patch.object(main.asyncio, "sleep", new=AsyncMock()):
            result = await playback_orchestration.configured().establish_effects_and_helper(request)

        self.assertTrue(result["graph_complete"])


    async def test_stereo_switch_final_readback_settles_over_transient_missing_links(self):
        """A stereo output-mode switch must not fail when the final diagnosis
        transiently misses the just-created native DSP-to-hardware front links."""
        overview = {
            "output_mode": {
                "mode": "stereo",
                "effective_output_key": "alsa_output.test",
            }
        }
        incomplete = {
            "mode": "stereo",
            "output_key": "alsa_output.test",
            "dsp_ports": True,
            "helper_ports": True,
            "helper_active": True,
            "helper_rate": 48000,
            "helper_rate_matches": True,
            "source_links": {},
            "source_links_complete": None,
            "links": {
                "fxroute_dsp:output_FL -> alsa_output.test:playback_FL": False,
                "fxroute_dsp:output_FR -> alsa_output.test:playback_FR": False,
            },
            "links_complete": False,
            "bypass_only": False,
            "port_identities": {
                "source": (),
                "source_target": (),
                "dsp": ("fxroute_dsp:output_FL", "fxroute_dsp:output_FR"),
                "helper": (),
                "output": ("alsa_output.test:playback_FL", "alsa_output.test:playback_FR"),
            },
            "signature": "stereo-links-missing",
        }
        complete = dict(incomplete)
        complete["links"] = {key: True for key in incomplete["links"]}
        complete["links_complete"] = True
        complete["signature"] = "stereo-links-stable"
        request = TransitionRequest(
            operation="output-mode-switch",
            source="local",
            target_rate=48000,
            target_url="/music/current.flac",
            target_track={"source": "local", "url": "/music/current.flac"},
            should_play=True,
            rate_change=False,
            reload_source=False,
            output_mode_target=overview,
            output_mode_config={"mode": "stereo"},
        )
        with patch.object(main.runtime, "dsp_runtime", None), patch.object(
            main, "dsp_manager", None
        ), patch.object(
            playback_orchestration.configured(), "playback_graph_diagnosis", new=AsyncMock(side_effect=[incomplete, incomplete, complete])
        ), patch.object(main.dsp_orchestrator, "sync_runtime", new=AsyncMock()        ), patch.object(playback_orchestration.configured(), "wait_for_dsp_output_ports", new=AsyncMock(return_value=True)), patch.object(
            samplerate, "reconcile_transition_sink_rate", new=AsyncMock(return_value=True)
        ), patch.object(
            pw_link_mod, "connect_ports", new=AsyncMock()
        ), patch.object(main.asyncio, "sleep", new=AsyncMock()):
            result = await playback_orchestration.configured().establish_effects_and_helper(request)

        self.assertTrue(result["graph_complete"])
        self.assertTrue(result["links_reconciled"])

    async def test_output_mode_dsp_reinitialization_stabilizes_before_gate_reopens(self):
        runtime = TransactionRuntime(dsp_reinitialized=True)
        coordinator = PlaybackTransitionCoordinator(runtime, gate_settle_seconds=0)

        result = await coordinator.execute(_request("output-mode-switch"))

        self.assertTrue(result.committed)
        self.assertLess(runtime.events.index("verify-output-mode"), runtime.events.index("dsp-stabilize"))
        self.assertLess(runtime.events.index("dsp-stabilize"), runtime.events.index("persist-output-mode"))
        self.assertLess(runtime.events.index("persist-output-mode"), runtime.events.index("mute:False"))


class ExternalOutputModeTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_qobuz_source_rate_falls_back_to_44100(self):
        self.assertEqual(
            playback_orchestration.configured().coordinator_source_rate(
                "qobuz", {"source": "qobuz"}
            ),
            44100,
        )

    async def test_output_mode_snapshot_captures_qobuz_transport(self):
        runtime = make_transition_runtime()
        request = TransitionRequest(
            operation="output-mode-switch",
            source="qobuz",
            target_rate=88200,
            should_play=True,
            output_mode_target={"output_mode": {"mode": "stereo"}},
            output_mode_config={"mode": "stereo"},
        )
        with patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 88200}
        ), patch.object(
            main, "get_audio_output_overview", return_value={"output_mode": {"mode": "stereo"}}
        ), patch.object(
            samplerate, "_load_raw_audio_output_mode", return_value={"mode": "stereo"}
        ), patch.object(
            main, "get_spotify_ui_state", new=AsyncMock(return_value={"status": "Paused"})
        ), patch.object(
            main, "get_qobuz_ui_state", new=AsyncMock(return_value={"status": "Playing"})
        ), patch.object(main, "dsp_manager", None):
            snapshot = await runtime.read_transition_snapshot(request)

        self.assertEqual(snapshot["qobuz"]["status"], "Playing")

    async def test_output_mode_restore_resumes_previously_playing_qobuz(self):
        runtime = make_transition_runtime()
        request = TransitionRequest(
            operation="output-mode-switch",
            source="qobuz",
            target_rate=88200,
            should_play=True,
            output_mode_target={"output_mode": {"mode": "stereo"}},
            output_mode_config={"mode": "stereo"},
        )
        with patch.object(
            main, "qobuz_play", new=AsyncMock(return_value={"status": "Playing"})
        ) as play, patch.object(
            main, "get_qobuz_ui_state", new=AsyncMock(return_value={"status": "Playing"})
        ):
            await runtime.restore_output_mode_transport(
                request,
                {"qobuz": {"status": "Playing"}},
                "tr-output-mode",
            )

        play.assert_awaited_once_with()

    async def test_output_mode_commit_requires_qobuz_sink_rate(self):
        runtime = make_transition_runtime()
        request = TransitionRequest(
            operation="output-mode-switch",
            source="qobuz",
            target_rate=44100,
            should_play=True,
            target_track={"source": "qobuz"},
            output_mode_target={"output_mode": {"mode": "stereo"}},
            output_mode_config={"mode": "stereo"},
        )
        diagnosis = {
            "links_complete": True,
            "signature": "qobuz-output-mode-stable",
        }
        with patch.object(
            main, "get_samplerate_status", return_value={
                "active_rate": 44100,
                "force_rate": 44100,
                "default_rate": 44100,
            }
        ), patch.object(
            playback_orchestration.configured(),
            "playback_graph_diagnosis",
            new=AsyncMock(return_value=diagnosis),
        ), patch.object(
            main, "get_qobuz_ui_state", new=AsyncMock(return_value={"status": "Playing"})
        ), patch.object(
            main,
            "_wait_for_qobuz_sink_input_samplerate",
            new=AsyncMock(return_value=None),
        ) as wait_rate:
            with self.assertRaisesRegex(RuntimeError, "Qobuz stream rate mismatch"):
                await runtime.verify_output_mode_runtime(request)

        wait_rate.assert_awaited_once_with(expected_rate=44100)


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
        with patch.object(main, "measurement_sr_session", SimpleNamespace(active=False, has_active_jobs=False)), patch.object(
            main, "prepare_audio_output_mode", return_value=target
        ), patch.object(main, "_coordinator_current_playback_context", new=AsyncMock(return_value={
            "source": "local",
            "target_url": "/music/current.flac",
            "target_track": {"source": "local", "url": "/music/current.flac"},
            "should_play": True,
        })), patch.object(main, "get_samplerate_status", return_value={"active_rate": 44100}), patch.object(
            main, "_run_coordinated_transition", run
        ), patch.object(main, "get_audio_output_overview", return_value=target["overview"]), patch.object(
            main, "with_subwoofer_derived_delays", side_effect=lambda value: value
        ), patch.object(main.runtime, "dsp_runtime", None), patch.object(
            main.dsp_orchestrator, "refresh_peak_monitor_after_effects_change", new=AsyncMock()
        ):
            await main.save_audio_output_mode_route(Request())

        run.assert_awaited_once()
        request = run.await_args.args[0]
        self.assertEqual(request.operation, "output-mode-switch")
        self.assertEqual(request.output_mode_target, target["overview"])
        self.assertEqual(request.output_mode_config, target["config"])

    async def test_running_dsp_output_mode_switch_uses_coordinator(self):
        class Request:
            async def json(self):
                return {"mode": "subwoofer-2.1", "subwoofer": {}}

        target = {
            "overview": {"output_mode": {"mode": "subwoofer-2.1"}},
            "config": {"mode": "subwoofer-2.1", "subwoofer": {}},
        }
        run = AsyncMock(return_value=SimpleNamespace(committed=True))
        dsp_runtime = SimpleNamespace(
            guarded_rebuild=AsyncMock(),
            snapshot=lambda: {},
        )
        with patch.object(
            main, "measurement_sr_session", SimpleNamespace(active=False, has_active_jobs=False)
        ), patch.object(
            main, "prepare_audio_output_mode", return_value=target
        ), patch.object(
            main.samplerate, "_load_audio_output_mode", return_value={"mode": "stereo"}
        ), patch.object(
            main, "_coordinator_current_playback_context", new=AsyncMock(return_value={
                "source": "local",
                "target_url": "/music/current.flac",
                "target_track": {"source": "local", "url": "/music/current.flac"},
                "should_play": True,
            })
        ), patch.object(
            main, "get_samplerate_status", return_value={"active_rate": 44100}
        ), patch.object(
            main, "_run_coordinated_transition", run
        ), patch.object(
            main, "get_audio_output_overview", return_value=target["overview"]
        ), patch.object(
            main, "persist_audio_output_mode"
        ) as persist, patch.object(
            main, "with_subwoofer_derived_delays", side_effect=lambda value: value
        ), patch.object(
            main.runtime, "dsp_runtime", dsp_runtime
        ), patch.object(
            main.dsp_orchestrator, "refresh_peak_monitor_after_effects_change", new=AsyncMock()
        ):
            await main.save_audio_output_mode_route(Request())

        run.assert_awaited_once()
        dsp_runtime.guarded_rebuild.assert_not_awaited()
        persist.assert_not_called()

    async def test_measurement_session_entry_submits_rate_change_to_coordinator(self):
        session = main.MeasurementSampleRateSession()
        result = SimpleNamespace(committed=True, target_rate=48000)
        run = AsyncMock(return_value=result)
        originals = {
            "measurement_sr_session": main.measurement_sr_session,
            "current_track_info": main.playback_state.current_track_info,
            "player_instance": main.runtime.player_instance,
        }
        try:
            main.measurement_sr_session = session
            main.playback_state.current_track_info = None
            main.runtime.player_instance = None
            with patch.object(measurement_session, "_capture_playback_state_before_measurement"), patch.object(
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
                setattr(main.runtime if hasattr(main.runtime, name) else main.playback_state if hasattr(main.playback_state, name) else main, name, value)

        request = run.await_args.args[0]
        self.assertEqual(request.operation, "measurement-entry")
        self.assertEqual(request.target_rate, 48000)
        self.assertTrue(request.rate_change)

    async def test_measurement_preflight_requires_route_ports_and_complete_graph(self):
        class Store:
            def _resolve_playback_target(self, *, overview=None):
                return {"target_name": "alsa_output.test"}

            def _build_measurement_playback_route(self, _node, target, *, overview=None):
                return {
                    "route": "direct-sink",
                    "playback_target_name": target["target_name"],
                }

            def _list_pw_ports(self, target):
                return [f"{target}:playback_FL", f"{target}:playback_FR"]

        coordinator = SimpleNamespace(
            transition_active=False,
            transition_blocked=False,
            gate=SimpleNamespace(failure_latched=False, closed=False),
        )
        reconcile = AsyncMock()
        coordinator.reconcile_measurement_session = reconcile
        with patch.object(main, "playback_transition_coordinator", coordinator), patch.object(
            main, "measurement_sr_session", SimpleNamespace(active=True)
        ), patch.object(
            main, "measurement_store", Store()
        ), patch.object(main, "get_samplerate_status", return_value={
            "active_rate": 48000,
            "force_rate": 48000,
        }), patch.object(playback_orchestration.configured(), "playback_graph_diagnosis", new=AsyncMock(return_value={
            "links_complete": True,
            "signature": "stable",
        })), patch.object(main, "get_audio_output_overview", return_value={"output_mode": {}}):
            await measurement_session._measurement_entry_preflight(48000)
        reconcile.assert_not_awaited()

    async def test_fresh_entry_preflight_keeps_rate_and_route_checks(self):
        class Store:
            ports_checked = False

            def _resolve_playback_target(self, *, overview=None):
                return {"target_name": "alsa_output.test"}

            def _build_measurement_playback_route(self, _node, target, *, overview=None):
                return {
                    "route": "direct-sink",
                    "playback_target_name": target["target_name"],
                }

            def _list_pw_ports(self, target):
                self.ports_checked = True
                return [f"{target}:playback_FL", f"{target}:playback_FR"]

        store = Store()
        diagnosis = AsyncMock()
        coordinator = SimpleNamespace(transition_blocked=False)
        with patch.object(main, "playback_transition_coordinator", coordinator), patch.object(
            main, "measurement_sr_session", SimpleNamespace(active=True)
        ), patch.object(main, "measurement_store", store), patch.object(
            main, "get_samplerate_status", return_value={
                "active_rate": 48000,
                "force_rate": 48000,
            }
        ), patch.object(
            playback_orchestration.configured(), "playback_graph_diagnosis", new=diagnosis
        ), patch.object(main, "get_audio_output_overview", return_value={"output_mode": {}}):
            await measurement_session._measurement_entry_preflight(
                48000,
                graph_already_verified=True,
            )

        diagnosis.assert_not_awaited()
        self.assertTrue(store.ports_checked)

    async def test_active_measurement_preflight_reconciles_only_link_loss_before_sweep(self):
        class Store:
            def _resolve_playback_target(self, *, overview=None):
                return {"target_name": "alsa_output.test"}

            def _build_measurement_playback_route(self, _node, target, *, overview=None):
                return {
                    "route": "direct-sink",
                    "playback_target_name": target["target_name"],
                }

            def _list_pw_ports(self, target):
                return [f"{target}:playback_FL", f"{target}:playback_FR"]

        reconcile = AsyncMock(return_value={
            "committed": True,
            "reconciled": True,
            "graph_complete": True,
            "stable_readbacks": 2,
        })
        coordinator = SimpleNamespace(
            transition_active=False,
            transition_blocked=False,
            gate=SimpleNamespace(failure_latched=False, closed=False),
            reconcile_measurement_session=reconcile,
        )
        diagnosis = {
            "mode": "subwoofer-2.2",
            "dsp_ports": True,
            "helper_ports": True,
            "helper_active": True,
            "helper_rate": 48000,
            "helper_rate_matches": True,
            "links_complete": False,
            "signature": "missing-dsp-helper",
        }
        with patch.object(main, "playback_transition_coordinator", coordinator), patch.object(
            main, "measurement_sr_session", SimpleNamespace(active=True)
        ), patch.object(main, "measurement_store", Store()), patch.object(
            main, "get_samplerate_status", return_value={
                "active_rate": 48000,
                "force_rate": 48000,
            }
        ), patch.object(
            playback_orchestration.configured(), "playback_graph_diagnosis", new=AsyncMock(return_value=diagnosis)
        ), patch.object(main, "get_audio_output_overview", return_value={"output_mode": {}}):
            await measurement_session._measurement_entry_preflight(48000)

        reconcile.assert_awaited_once_with(
            target_rate=48000,
            initial_graph=diagnosis,
        )

    async def test_measurement_entry_and_output_mode_endpoint_sources_have_no_direct_graph_mutators(self):
        output_mode_source = inspect.getsource(main.save_audio_output_mode_route)
        start_source = inspect.getsource(main.MeasurementSampleRateSession._start_locked)
        # Every real mode switch must still enter the Coordinator's muted
        # transition and must never load presets from the endpoint directly.
        self.assertIn("_run_coordinated_transition", output_mode_source)
        self.assertIn("prepare_audio_output_mode", output_mode_source)
        self.assertNotIn("load_preset", output_mode_source)
        self.assertNotIn("set_audio_output_mode(", output_mode_source)
        # The sole exception: a same-mode request is a pure DSP parameter edit
        # (crossover/level/alignment/polarity/highpass) that rebuilds no
        # routing, samplerate or graph topology, so it restores the
        # pre-coordinator direct sync without closing the output gate.
        self.assertIn("target_mode == current_mode", output_mode_source)
        self.assertIn("persist_audio_output_mode", output_mode_source)
        self.assertIn("dsp_orchestrator.sync_runtime", output_mode_source)
        self.assertIn("operation=\"measurement-entry\"", start_source)
        self.assertIn("_run_coordinated_transition", start_source)
        self.assertNotIn("_set_pipewire_force_rate", start_source)
        self.assertNotIn("dsp_orchestrator.sync_runtime", start_source)

    def test_sample_rate_policy_reuses_settings_selector_and_coordinator(self):
        index = (pathlib.Path(__file__).resolve().parents[1] / "static" / "index.html").read_text()
        app = (pathlib.Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
        samplerate_source = "\n".join(
            path.read_text()
            for path in sorted((pathlib.Path(__file__).resolve().parents[1] / "audio" / "samplerate").glob("*.py"))
        )
        orchestration_source = (pathlib.Path(__file__).resolve().parents[1] / "playback/orchestration.py").read_text()
        self.assertIn("settings-samplerate-select", index)
        self.assertIn("Sample Rate", index)
        self.assertIn("saveSampleRatePolicy", app)
        self.assertNotIn("set_pipewire_default_rate_selection", samplerate_source)
        self.assertNotIn("_render_pipewire_clock_rate_dropin", samplerate_source)
        self.assertIn('operation="sample-rate-policy"', orchestration_source)
        self.assertIn("_transition_sample_rate_policy", inspect.getsource(main.save_audio_samplerate_policy))
        self.assertIn("run_transition", inspect.getsource(main._transition_sample_rate_policy))
        self.assertIn("/api/audio/samplerate", app)


class MeasurementSessionRuntimeReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_marks_only_healthy_subwoofer_input_loss_repairable(self):
        runtime = make_transition_runtime()
        diagnosis = {
            "mode": "subwoofer-2.2",
            "output_key": "alsa_output.test",
            "dsp_ports": True,
            "helper_ports": True,
            "helper_active": True,
            "helper_rate": 48000,
            "helper_rate_matches": True,
            "links": {
                "fxroute_dsp_sink:monitor_FL -> fxroute_dsp:input_1": False,
                "fxroute_dsp_sink:monitor_FR -> fxroute_dsp:input_2": False,
                "fxroute_dsp:output_1 -> alsa_output.test:playback_FL": True,
                "fxroute_dsp:output_2 -> alsa_output.test:playback_FR": True,
                "fxroute_dsp:output_3 -> alsa_output.test:playback_RL": True,
                "fxroute_dsp:output_4 -> alsa_output.test:playback_RR": True,
            },
            "links_complete": False,
            "signature": "missing-dsp-helper",
        }
        with patch.object(
            main,
            "get_samplerate_status",
            return_value={"active_rate": 48000, "force_rate": 48000},
        ), patch.object(
            playback_orchestration.configured(), "playback_graph_diagnosis", new=AsyncMock(return_value=diagnosis)
        ):
            readback = await runtime.read_measurement_session_graph(48000)

        self.assertTrue(readback["measurement_rate_aligned"])
        self.assertTrue(readback["repairable_link_loss"])

        invalid_rate = dict(readback)
        invalid_rate["active_rate"] = 44100
        invalid_rate["measurement_rate_aligned"] = False
        self.assertFalse(
            playback_orchestration.configured().measurement_session_link_loss_is_repairable(
                invalid_rate,
                target_rate=48000,
            )
        )

        invalid_helper_link = dict(readback)
        invalid_helper_link["links"] = dict(readback["links"])
        invalid_helper_link["links"]["unknown:output -> unknown:input"] = False
        self.assertFalse(
            playback_orchestration.configured().measurement_session_link_loss_is_repairable(
                invalid_helper_link,
                target_rate=48000,
            )
        )


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
            with patch.object(samplerate.overview, "_audio_output_mode_path", return_value=path), patch.object(
                samplerate.overview, "get_audio_output_overview", return_value=overview
            ):
                target = samplerate.prepare_audio_output_mode("stereo")
                self.assertFalse(path.exists())
                samplerate.persist_audio_output_mode(target["config"])
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text() and target["config"]["mode"], "stereo")


if __name__ == "__main__":
    unittest.main()
