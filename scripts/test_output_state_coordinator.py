#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""v2 coordinator path: planned diagnosis, staging, commit, verify, rollback."""

import asyncio
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import main
import playback.orchestration as playback_orchestration
from playback.orchestration import PlaybackOrchestrator, PlaybackOrchestrationDeps
from playback.transition import PlaybackTransitionFailure, TransitionRequest
from playback_transition_test_support import make_transition_runtime
from audio.output_service import OutputService, OutputServiceDeps
from audio.output_state import set_bank_preset, set_mode_routing
from audio.output_state_store import OutputStateStore, StateConflictError
from dsp.manager import DSPManager
from dsp.runtime import DSPRuntimeConfig, PlannedSyncTarget, _contains_link

OUTPUT_KEY = "A"
PORTS = [f"playback_AUX{i}" for i in range(6)]
PLANNED = [[index + 1, port] for index, port in enumerate(PORTS)]


def orchestrator(overrides):
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
            current_track_info={}, latest_qobuz_state={}, current_playback_owner=None),
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
        contains_link=_contains_link,
        helper_argument_sample_rate=lambda _snapshot: None,
        sync_preset_for_samplerate=_none,
        sync_runtime=_none,
        reconcile_sink_rate=_none,
        load_dsp_preset=_none,
        sleep=lambda _delay: asyncio.sleep(0),
        pipewire_poll_interval_ms=10,
        dsp_port_timeout_ms=5,
        post_start_readbacks=2,
        output_mode_subwoofer_modes=frozenset({"subwoofer-2.1", "subwoofer-2.2"}),
        output_mode_stereo="stereo",
    )
    return PlaybackOrchestrator(replace(deps, **overrides))


def v2_overview(*, planned=PLANNED):
    return {
        "selected_output": {"key": OUTPUT_KEY, "channels": 6},
        "output_mode": {"mode": "crossover", "effective_output_key": OUTPUT_KEY,
                        "effective_output_channels": 6,
                        "hardware_playback_ports": list(PORTS),
                        "planned_routes": planned},
    }


def graph_text(edges):
    lines = ["fxroute_dsp:input_1", "fxroute_dsp:input_2",
             "fxroute_dsp_sink:monitor_FL", "fxroute_dsp_sink:monitor_FR"]
    lines += [f"fxroute_dsp:output_{index + 1}" for index in range(6)]
    lines += [f"{OUTPUT_KEY}:{port}" for port in PORTS]
    body = "\n".join(lines)
    body += "\n" + "\n".join(f"{source} -> {target}" for source, target in edges)
    return body


def v2_edges():
    return ([("fxroute_dsp_sink:monitor_FL", "fxroute_dsp:input_1"),
             ("fxroute_dsp_sink:monitor_FR", "fxroute_dsp:input_2")] +
            [(f"fxroute_dsp:output_{signal}", f"{OUTPUT_KEY}:{port}")
             for signal, port in PLANNED])


class PlannedDiagnosisTests(unittest.IsolatedAsyncioTestCase):
    async def diagnose(self, overview, snapshot, link_text):
        async def run_pw_link(command):
            if command == "-io":
                return graph_text([])
            return link_text

        orchestrator_instance = orchestrator({
            "run_pw_link_command": run_pw_link,
            "helper_argument_sample_rate": lambda _snapshot: 48000,
            "get_dsp_snapshot": lambda: snapshot,
        })
        return await orchestrator_instance.playback_graph_diagnosis(
            overview, target_rate=48000)

    def snapshot(self, *, fingerprint=None, routes=None, ports=PORTS):
        return {"active": True,
                "config": {"hardware_ports": list(ports), "plan_fingerprint": fingerprint,
                           "output_routes": routes}}

    async def test_planned_routes_drive_expectations(self):
        diagnosis = await self.diagnose(v2_overview(), self.snapshot(), graph_text(v2_edges()))
        self.assertTrue(diagnosis["links_complete"], diagnosis["signature"])
        self.assertEqual(len(diagnosis["output_targets"]), 6)
        self.assertIn(f"{OUTPUT_KEY}:{PORTS[5]}", diagnosis["output_targets"])

    async def test_snapshot_fingerprint_routes_cover_steady_state(self):
        diagnosis = await self.diagnose(
            v2_overview(planned=None), self.snapshot(fingerprint="fp", routes=[list(e) for e in PLANNED]),
            graph_text(v2_edges()))
        self.assertTrue(diagnosis["links_complete"], diagnosis["signature"])

    async def test_legacy_snapshot_routes_are_ignored(self):
        diagnosis = await self.diagnose(
            v2_overview(planned=None),
            self.snapshot(fingerprint=None, routes=[list(e) for e in PLANNED], ports=[]),
            graph_text(v2_edges()))
        self.assertFalse(diagnosis["links_complete"])
        self.assertEqual(len(list(diagnosis["output_targets"])), 2)

    async def test_malformed_planned_routes_fall_back_without_crashing(self):
        diagnosis = await self.diagnose(
            v2_overview(planned=[["x"]]), self.snapshot(), graph_text(v2_edges()))
        self.assertFalse(diagnosis["links_complete"])


class StagingBranchTests(unittest.IsolatedAsyncioTestCase):
    async def test_v2_target_uses_plan_sync_and_skips_legacy_repairs(self):
        sync_plan = AsyncMock()
        sync_legacy = AsyncMock()
        sync_preset = AsyncMock()
        load_preset = AsyncMock()
        manager = mock.MagicMock()
        manager.load_compare_state.return_value = {"presetA": "X", "presetB": "", "activeSide": "A"}
        manager.get_active_preset.return_value = "Y"
        instance = orchestrator({
            "sync_plan_runtime": sync_plan,
            "sync_runtime": sync_legacy,
            "sync_preset_for_samplerate": sync_preset,
            "load_dsp_preset": load_preset,
            "reconcile_sink_rate": AsyncMock(return_value=True),
            "get_dsp_manager": lambda: manager,
        })
        repair = AsyncMock()
        sub = AsyncMock()
        instance.repair_stereo_output_links_once = repair
        instance.reconcile_subwoofer_links_only = sub
        canned = {"dsp_ports": True, "bypass_only": False, "links_complete": True,
                  "signature": "sig"}
        sentinel = object()
        request = TransitionRequest(
            operation="output-mode-switch", source="local", target_rate=48000,
            should_play=False, output_mode_target=v2_overview(),
            output_state_transition={"target": sentinel})
        with mock.patch.object(instance, "playback_graph_diagnosis",
                               new=AsyncMock(return_value=dict(canned))), \
             mock.patch.object(instance, "wait_for_dsp_output_ports",
                               new=AsyncMock(return_value=True)):
            result = await instance.establish_effects_and_helper(request)
        sync_plan.assert_awaited_once()
        self.assertIs(sync_plan.await_args.args[0], sentinel)
        sync_legacy.assert_not_awaited()
        sync_preset.assert_not_awaited()
        load_preset.assert_not_awaited()
        repair.assert_not_awaited()
        sub.assert_not_awaited()
        self.assertTrue(result["helper_rebuilt"])
        self.assertTrue(result["links_reconciled"])

    async def test_legacy_request_keeps_legacy_sync(self):
        sync_plan = AsyncMock()
        sync_legacy = AsyncMock()
        instance = orchestrator({
            "sync_plan_runtime": sync_plan,
            "sync_runtime": sync_legacy,
            "reconcile_sink_rate": AsyncMock(return_value=True),
            "get_dsp_manager": lambda: None,
        })
        canned = {"dsp_ports": True, "bypass_only": False, "links_complete": True,
                  "signature": "sig"}
        request = TransitionRequest(
            operation="output-mode-switch", source="local", target_rate=48000,
            should_play=False, output_mode_target=v2_overview())
        with mock.patch.object(instance, "playback_graph_diagnosis",
                               new=AsyncMock(return_value=dict(canned))), \
             mock.patch.object(instance, "wait_for_dsp_output_ports",
                               new=AsyncMock(return_value=True)):
            await instance.establish_effects_and_helper(request)
        sync_legacy.assert_awaited_once()
        sync_plan.assert_not_awaited()


def make_service(directory, manager):
    return OutputService(OutputServiceDeps(
        store=OutputStateStore(Path(directory) / "output-state.json"),
        preset_loader=manager.preset_store.read,
        resolve_ir=lambda kernel: (_ for _ in ()).throw(AssertionError(kernel)),
        measurement_active=lambda: False,
    ))


def make_manager(directory):
    manager = DSPManager(home=Path(directory) / "home")
    manager.preset_store.write("Room", {"schema": "fxroute.dsp.preset", "version": 1, "chain": []})
    return manager


def seed_sub_state(service):
    return service.apply(
        lambda state: set_mode_routing(state, "stereo", "A", ["main_l", "main_r", "sub1", "sub1"]),
        expected_revision=0)


def v2_payload(service, candidate, previous, *, expected_revision=1, fingerprint=None,
               target=None, previous_target=None):
    if fingerprint is None:
        fingerprint = service.fingerprint(candidate, output_key=OUTPUT_KEY, channels=4,
                                          sample_rate_hz=48000)
    return {"candidate_state": candidate, "previous_state": previous,
            "expected_revision": expected_revision, "fingerprint": fingerprint,
            "target": target, "previous_target": previous_target,
            "output_key": OUTPUT_KEY, "channels": 4}


def v2_request(payload, *, target_rate=48000):
    return TransitionRequest(
        operation="output-mode-switch", source="local", target_rate=target_rate,
        should_play=False, output_mode_target=v2_overview(),
        output_state_transition=payload)


def plan_target(service, manager, state, fingerprint="fp"):
    plan = service.compile_plan(state, output_key="A", channels=4, sample_rate_hz=48000)
    layout = service.compile_layout(plan)
    config = DSPRuntimeConfig.from_plan(
        plan, layout=layout, output_key="A", sample_rate_hz=48000,
        hardware_ports=[f"playback_AUX{i}" for i in range(4)],
        plan_fingerprint=fingerprint)
    text = manager.compile_engine_text(
        [dict(entry) for entry in layout], preset_name=plan["global"]["preset"],
        sample_rate_hz=48000, extras_override=plan["global"]["extras"])
    return PlannedSyncTarget(config=config, text=text)


class CommitV2Tests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)
        self.base = seed_sub_state(self.service)
        self.candidate = set_bank_preset(self.base, "stereo", "sub1", preset="Room")

    def test_commit_persists_candidate_with_revision(self):
        runtime = make_transition_runtime()
        with mock.patch.object(main, "get_output_service", return_value=self.service):
            result = asyncio.run(runtime.commit_output_mode_runtime(
                v2_request(v2_payload(self.service, self.candidate, self.base))))
        self.assertTrue(result["output_mode_persisted"])
        self.assertEqual(result["output_state_revision"], 2)
        self.assertEqual(result["output_fingerprint"],
                         self.service.fingerprint(self.candidate, output_key="A",
                                                  channels=4, sample_rate_hz=48000))
        self.assertEqual(self.service.load()["revision"], 2)

    def test_commit_conflict_propagates(self):
        runtime = make_transition_runtime()
        self.service.apply(lambda state: set_bank_preset(state, "stereo", "global", preset="Room"),
                           expected_revision=1)
        with mock.patch.object(main, "get_output_service", return_value=self.service):
            with self.assertRaises(StateConflictError):
                asyncio.run(runtime.commit_output_mode_runtime(
                    v2_request(v2_payload(self.service, self.candidate, self.base))))
        self.assertEqual(self.service.load()["revision"], 2)

    def test_commit_without_service_fails(self):
        runtime = make_transition_runtime()
        with mock.patch.object(main, "get_output_service", return_value=None):
            with self.assertRaises(RuntimeError):
                asyncio.run(runtime.commit_output_mode_runtime(
                    v2_request(v2_payload(self.service, self.candidate, self.base))))


class FinalizeV2Tests(unittest.TestCase):
    def finalize(self, request):
        runtime = make_transition_runtime()
        sub = AsyncMock()
        complete = {"links_complete": True, "signature": "sig"}
        with mock.patch.object(runtime, "_deps") as deps:
            deps.coordinator_reconcile_subwoofer_links_only = sub
            deps.playback_graph_diagnosis = AsyncMock(return_value=dict(complete))
            result = asyncio.run(runtime.finalize_output_mode_graph_after_gate_open(request))
        return result, sub

    def test_finalize_skips_legacy_sub_reconcile_for_v2(self):
        request = v2_request({"candidate_state": {}, "expected_revision": 1,
                              "fingerprint": "fp", "target": {"t": 1},
                              "output_key": "A", "channels": 4})
        request = replace(request, output_mode_target=v2_overview())
        result, sub = self.finalize(request)
        self.assertTrue(result["graph_complete"])
        sub.assert_not_awaited()

    def test_finalize_keeps_legacy_sub_reconcile_without_v2(self):
        from audio.samplerate import OUTPUT_MODE_SUBWOOFER_21
        overview = v2_overview()
        overview["output_mode"] = {**overview["output_mode"], "mode": OUTPUT_MODE_SUBWOOFER_21}
        del overview["output_mode"]["planned_routes"]
        request = TransitionRequest(
            operation="output-mode-switch", source="local", target_rate=48000,
            should_play=False, output_mode_target=overview)
        _, sub = self.finalize(request)
        sub.assert_awaited_once()


class VerifyFingerprintTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)
        self.base = seed_sub_state(self.service)
        self.candidate = set_bank_preset(self.base, "stereo", "sub1", preset="Room")

    def verify(self, payload, fingerprint):
        runtime = make_transition_runtime()
        fake = mock.MagicMock()
        fake.snapshot.return_value = {"config": {"plan_fingerprint": fingerprint}}
        with mock.patch.object(main, "get_samplerate_status",
                               return_value={"active_rate": 48000, "force_rate": 48000}), \
             mock.patch.object(main.runtime, "dsp_runtime", fake), \
             mock.patch.object(playback_orchestration.configured(), "playback_graph_diagnosis",
                               new=AsyncMock(return_value={"links_complete": True,
                                                           "signature": "sig"})):
            return asyncio.run(runtime.verify_output_mode_runtime(v2_request(payload)))

    def test_matching_fingerprint_commits(self):
        result = self.verify(v2_payload(self.service, self.candidate, self.base,
                                        fingerprint="fp-new"), "fp-new")
        self.assertTrue(result["committed"])
        self.assertEqual(result["output_fingerprint"], "fp-new")

    def test_mismatching_fingerprint_raises(self):
        with self.assertRaises(RuntimeError):
            self.verify(v2_payload(self.service, self.candidate, self.base,
                                   fingerprint="fp-new"), "fp-old")


class RollbackV2Tests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)
        self.base = seed_sub_state(self.service)
        self.candidate = set_bank_preset(self.base, "stereo", "sub1", preset="Room")
        self.previous_target = plan_target(self.service, self.manager, self.base)
        self.runtime = make_transition_runtime()

    def synced(self):
        fake = mock.MagicMock()
        fake.sync_rendered = AsyncMock()
        return fake

    def rollback(self, payload):
        with mock.patch.object(main, "get_output_service", return_value=self.service), \
             mock.patch.object(playback_orchestration.configured(), "playback_graph_diagnosis",
                               new=AsyncMock(return_value={"links_complete": True,
                                                           "signature": "sig"})):
            return asyncio.run(self.runtime.rollback_output_mode_runtime(
                v2_request(payload), snapshot={}))

    def test_rollback_reverts_and_resyncs_previous(self):
        self.service.commit(self.candidate, expected_revision=1)
        fake = self.synced()
        with mock.patch.object(main.runtime, "dsp_runtime", fake):
            self.rollback(v2_payload(self.service, self.candidate, self.base,
                                     previous_target=self.previous_target))
        self.assertEqual(self.service.load()["revision"], 3)
        self.assertEqual(self.service.load()["modes"]["stereo"]["banks"]["sub1"]["preset"], "Neutral")
        fake.sync_rendered.assert_awaited_once_with(self.previous_target)

    def test_concurrent_winner_skips_rollback(self):
        self.service.commit(self.candidate, expected_revision=1)
        self.service.apply(lambda state: set_bank_preset(state, "stereo", "global", preset="Room"),
                           expected_revision=2)
        fake = self.synced()
        with mock.patch.object(main.runtime, "dsp_runtime", fake):
            self.rollback(v2_payload(self.service, self.candidate, self.base,
                                     previous_target=self.previous_target))
        self.assertEqual(self.service.load()["revision"], 3)
        fake.sync_rendered.assert_not_awaited()

    def test_unlanded_commit_resyncs_graph_without_revert(self):
        fake = self.synced()
        with mock.patch.object(main.runtime, "dsp_runtime", fake):
            self.rollback(v2_payload(self.service, self.candidate, self.base,
                                     previous_target=self.previous_target))
        self.assertEqual(self.service.load()["revision"], 1)
        fake.sync_rendered.assert_awaited_once_with(self.previous_target)

    def test_rollback_without_service_fails(self):
        with mock.patch.object(main, "get_output_service", return_value=None):
            with self.assertRaises(RuntimeError):
                asyncio.run(self.runtime.rollback_output_mode_runtime(
                    v2_request(v2_payload(self.service, self.candidate, self.base)),
                    snapshot={}))

    def test_rollback_resync_failure_propagates_after_revert(self):
        self.service.commit(self.candidate, expected_revision=1)
        fake = self.synced()
        with mock.patch.object(main, "get_output_service", return_value=self.service), \
             mock.patch.object(main.runtime, "dsp_runtime", fake), \
             mock.patch.object(playback_orchestration.configured(), "playback_graph_diagnosis",
                               new=AsyncMock(return_value={"links_complete": False,
                                                           "signature": "bad"})):
            with self.assertRaises(RuntimeError):
                asyncio.run(self.runtime.rollback_output_mode_runtime(
                    v2_request(v2_payload(self.service, self.candidate, self.base,
                                          previous_target=self.previous_target)),
                    snapshot={}))
        self.assertEqual(self.service.load()["revision"], 3)


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def route_context(service, manager, run):
    stack = mock.patch.multiple(
        main,
        get_output_service=mock.MagicMock(return_value=service),
        measurement_sr_session=mock.MagicMock(has_active_jobs=False),
        get_audio_output_overview=mock.MagicMock(return_value={
            "selected_output": {"key": OUTPUT_KEY, "channels": 4},
            "output_mode": {"effective_output_key": OUTPUT_KEY, "effective_output_channels": 4,
                            "hardware_playback_ports": [f"playback_AUX{i}" for i in range(4)]},
        }),
        get_samplerate_status=mock.MagicMock(return_value={"active_rate": 48000}),
        _require_dsp_manager=mock.MagicMock(return_value=manager),
        _coordinator_current_playback_context=mock.AsyncMock(return_value={
            "source": "local", "target_url": None, "target_track": {}, "should_play": False}),
        _run_coordinated_transition=run,
    )
    return stack


class MainTopologyBranchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)
        seed_sub_state(self.service)

    def test_topology_change_runs_coordinator_with_v2_payload(self):
        run = AsyncMock()
        guarded = AsyncMock()
        with route_context(self.service, self.manager, run), \
             mock.patch.object(main.runtime, "dsp_runtime", mock.MagicMock(
                 snapshot=lambda: {"output_gain_db": 0.0},
                 guarded_rebuild_rendered=guarded, sync_rendered=AsyncMock())):
            result = asyncio.run(main.apply_audio_output_state(FakeRequest({
                "expected_revision": 1,
                "mutation": {"kind": "set_routing", "mode": "stereo",
                             "assignments": ["main_l", "main_r", "sub1", "sub2"]},
            })))
        run.assert_awaited_once()
        guarded.assert_not_awaited()
        request = run.await_args.args[0]
        self.assertEqual(request.operation, "output-mode-switch")
        payload = request.output_state_transition
        self.assertEqual(payload["expected_revision"], 1)
        self.assertEqual(payload["fingerprint"], result["fingerprint"])
        self.assertIn("candidate_state", payload)
        self.assertIn("target", payload)
        planned = request.output_mode_target["output_mode"]["planned_routes"]
        self.assertEqual(planned, [[1, "playback_AUX0"], [2, "playback_AUX1"],
                                   [3, "playback_AUX2"], [4, "playback_AUX3"]])
        self.assertTrue(result["live_applied"])
        self.assertIsNone(result["live_reason"])
        # The coordinator is mocked, so nothing really committed: the
        # response revision is read back after the transition.
        self.assertEqual(result["revision"], self.service.load()["revision"])
        self.assertEqual(result["revision"], 1)
        candidate_roles = payload["candidate_state"]["modes"]["stereo"]["routing"]["A"]
        self.assertIn("sub2", candidate_roles)

    def test_coordinator_failure_leaves_candidate_uncommitted(self):
        async def fail(_request):
            raise PlaybackTransitionFailure("boom", transition_id="t", stage="output-mode-persist")

        with route_context(self.service, self.manager, fail):
            with self.assertRaises(main.HTTPException) as ctx:
                asyncio.run(main.apply_audio_output_state(FakeRequest({
                    "expected_revision": 1,
                    "mutation": {"kind": "set_routing", "mode": "stereo",
                                 "assignments": ["main_l", "main_r", "sub1", "sub2"]},
                })))
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(self.service.load()["revision"], 1)

    def test_coordinator_conflict_maps_to_409(self):
        async def conflict(_request):
            failure = PlaybackTransitionFailure("conflict", transition_id="t",
                                                stage="output-mode-persist")
            failure.__cause__ = StateConflictError("Output state changed")
            raise failure

        with route_context(self.service, self.manager, conflict):
            with self.assertRaises(main.HTTPException) as ctx:
                asyncio.run(main.apply_audio_output_state(FakeRequest({
                    "expected_revision": 1,
                    "mutation": {"kind": "set_routing", "mode": "stereo",
                                 "assignments": ["main_l", "main_r", "sub1", "sub2"]},
                })))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_sync_plan_runtime_binding_forwards_target(self):
        fake = mock.MagicMock()
        fake.sync_rendered = AsyncMock()
        with mock.patch.object(main.runtime, "dsp_runtime", fake):
            asyncio.run(main._sync_plan_runtime({"config": "c", "text": "t"}))
        fake.sync_rendered.assert_awaited_once()
        target = fake.sync_rendered.await_args.args[0]
        self.assertEqual((target.config, target.text), ("c", "t"))

    def test_sync_plan_runtime_without_runtime_fails(self):
        with mock.patch.object(main.runtime, "dsp_runtime", None):
            with self.assertRaises(RuntimeError):
                asyncio.run(main._sync_plan_runtime({"config": "c", "text": "t"}))


if __name__ == "__main__":
    unittest.main()
