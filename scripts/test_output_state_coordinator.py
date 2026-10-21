#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""v2 coordinator path: planned diagnosis, staging, commit, verify, rollback."""

import asyncio
import inspect
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
from audio.output_state import set_bank_preset, set_crossover, set_mode_routing, switch_mode
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
        sync_runtime=_none,
        reconcile_sink_rate=_none,
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

    async def test_planned_routes_ignore_overview_mode_label(self):
        """Task-3 contract: equal plans verify identically whatever the label.

        With staged plan routes the diagnosis must produce the same link
        expectations for every legacy mode label; the label only echoes in
        the ``mode`` field, never in targets/ports/completeness.
        """
        expected = [f"{OUTPUT_KEY}:{port}" for _, port in PLANNED]
        for label in ("stereo", "subwoofer-2.1", "subwoofer-2.2", "crossover", ""):
            overview = v2_overview()
            overview["output_mode"] = {**overview["output_mode"], "mode": label}
            diagnosis = await self.diagnose(
                overview, self.snapshot(), graph_text(v2_edges()))
            self.assertTrue(diagnosis["links_complete"], (label, diagnosis["signature"]))
            self.assertEqual(list(diagnosis["output_targets"]), expected, label)
            self.assertEqual(diagnosis["mode"], label)

    async def test_malformed_planned_routes_fall_back_without_crashing(self):
        diagnosis = await self.diagnose(
            v2_overview(planned=[["x"]]), self.snapshot(), graph_text(v2_edges()))
        self.assertFalse(diagnosis["links_complete"])


class StagingBranchTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_retains_runtime_repair_after_its_error_changes_the_diagnosis(self):
        for mode in ('stereo', 'surround'):
            with self.subTest(mode=mode):
                attempts = 0
                error = None
                async def repair():
                    nonlocal attempts, error
                    attempts += 1
                    if attempts == 1:
                        error = 'Native DSP output links did not verify after repair'
                        raise RuntimeError(error)
                    error = None
                instance = orchestrator({
                    'get_dsp_runtime': lambda: SimpleNamespace(reclean_direct_dsp_links=repair)})
                async def diagnosis(*args, **kwargs):
                    return {'dsp_ports': True, 'helper_ports': True, 'links': {},
                            'helper_active': error is None, 'bypass_only': attempts == 0,
                            'links_complete': attempts > 0 and error is None, 'signature': mode}
                request = TransitionRequest(operation='play', source='local', target_rate=48000,
                    graph_only=True, audio_overview={'output_mode': {'mode': mode}})
                with mock.patch.object(instance, 'playback_graph_diagnosis', side_effect=diagnosis):
                    result = await instance.establish_effects_and_helper(request)
                self.assertTrue(result['graph_complete'])
                self.assertEqual(attempts, 2)

    async def test_effects_link_repairs_retry_exceptions_and_remain_bounded(self):
        for failures in (1, 99):
            with self.subTest(failures=failures):
                attempts = 0
                complete = False

                async def repair():
                    nonlocal attempts, complete
                    attempts += 1
                    if attempts <= failures:
                        raise RuntimeError('Native DSP output links did not verify after repair')
                    complete = True

                instance = orchestrator({
                    'get_dsp_runtime': lambda: SimpleNamespace(reclean_direct_dsp_links=repair),
                    'get_dsp_snapshot': lambda: {'active': True},
                    'helper_argument_sample_rate': lambda _: 48000,
                })
                async def diagnosis(*args, **kwargs):
                    return {'dsp_ports': True, 'helper_ports': True, 'links': {},
                            'bypass_only': True, 'links_complete': complete, 'signature': 'test'}
                request = TransitionRequest(operation='play', source='local', target_rate=48000,
                    audio_overview={'output_mode': {'mode': 'subwoofer-2.1'}})
                with mock.patch.object(instance, 'playback_graph_diagnosis', side_effect=diagnosis), \
                        mock.patch.object(instance, 'wait_for_dsp_output_ports', return_value=True):
                    if failures == 1:
                        result = await instance.establish_effects_and_helper(request)
                        self.assertTrue(result['graph_complete'])
                        self.assertEqual(attempts, 2)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'canonical topology') as raised:
                            await instance.establish_effects_and_helper(request)
                        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
                        self.assertEqual(attempts, 4)

    async def test_v2_target_uses_plan_sync_and_skips_runtime_repairs(self):
        sync_plan = AsyncMock()
        sync_runtime = AsyncMock()
        instance = orchestrator({
            "sync_plan_runtime": sync_plan,
            "sync_runtime": sync_runtime,
            "reconcile_sink_rate": AsyncMock(return_value=True),
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
        sync_runtime.assert_not_awaited()
        repair.assert_not_awaited()
        sub.assert_not_awaited()
        self.assertTrue(result["helper_rebuilt"])
        self.assertTrue(result["links_reconciled"])

    async def test_mode_switch_without_a_planned_target_is_refused(self):
        sync_plan = AsyncMock()
        sync_runtime = AsyncMock()
        instance = orchestrator({
            "sync_plan_runtime": sync_plan,
            "sync_runtime": sync_runtime,
            "reconcile_sink_rate": AsyncMock(return_value=True),
        })
        canned = {"dsp_ports": True, "bypass_only": False, "links_complete": True,
                  "signature": "sig"}
        request = TransitionRequest(
            operation="output-mode-switch", source="local", target_rate=48000,
            should_play=False, output_mode_target=v2_overview())
        with mock.patch.object(instance, "playback_graph_diagnosis",
                               new=AsyncMock(return_value=dict(canned))):
            with self.assertRaisesRegex(RuntimeError, "requires a planned target"):
                await instance.establish_effects_and_helper(request)
        sync_runtime.assert_not_awaited()
        sync_plan.assert_not_awaited()

    async def test_missing_engine_starts_through_the_runtime_sync(self):
        sync_runtime = AsyncMock()
        instance = orchestrator({
            "sync_runtime": sync_runtime,
            "reconcile_sink_rate": AsyncMock(return_value=True),
        })
        diagnoses = [{"dsp_ports": False, "helper_ports": False, "links": {},
                      "links_complete": False, "signature": "missing"},
                     {"dsp_ports": True, "bypass_only": False, "links_complete": True,
                      "signature": "sig"}]
        request = TransitionRequest(operation="play", source="local", target_rate=48000,
                                    should_play=True)
        with mock.patch.object(instance, "playback_graph_diagnosis",
                               new=AsyncMock(side_effect=lambda *_a, **_k: dict(diagnoses.pop(0) if len(diagnoses) > 1 else diagnoses[0]))), \
             mock.patch.object(instance, "wait_for_dsp_output_ports",
                               new=AsyncMock(return_value=True)), \
             mock.patch.object(instance, "repair_stereo_output_links_once", new=AsyncMock()):
            result = await instance.establish_effects_and_helper(request)
        self.assertEqual(sync_runtime.await_args_list[0].kwargs,
                          {"reason": "coordinator-play", "ownership": main.DspSyncOwnership.UNOWNED})
        self.assertTrue(result["dsp_reinitialized"])


def make_service(directory, manager):
    return OutputService(OutputServiceDeps(
        store=OutputStateStore(Path(directory) / "output-state.json"),
        preset_loader=manager.read_preset,
        resolve_ir=lambda _bank, kernel: (_ for _ in ()).throw(AssertionError(kernel)),
        measurement_active=lambda: False,
    ))


def make_manager(directory):
    manager = DSPManager(home=Path(directory) / "home")
    for bank in ("sub1", "global"):
        manager.library(bank, "stereo-sub").write(
            "Room", {"schema": "fxroute.dsp.preset", "version": 1, "chain": []})
    return manager


def seed_sub_state(service):
    return service.apply(
        lambda state: switch_mode(set_mode_routing(state, "stereo-sub", "A", ["main_l", "main_r", "sub1", "sub1"]), "stereo-sub"),
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
        [dict(entry) for entry in layout], preset_name=plan["global"]["preset"], mode=plan["mode"],
        sample_rate_hz=48000, extras_override=plan["global"]["extras"])
    return PlannedSyncTarget(config=config, text=text)


class CommitV2Tests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)
        self.base = seed_sub_state(self.service)
        self.candidate = set_bank_preset(self.base, "stereo-sub", "sub1", preset="Room")

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
        self.service.apply(lambda state: set_bank_preset(state, "stereo-sub", "global", preset="Room"),
                           expected_revision=1)
        with mock.patch.object(main, "get_output_service", return_value=self.service):
            with self.assertRaises(StateConflictError):
                asyncio.run(runtime.commit_output_mode_runtime(
                    v2_request(v2_payload(self.service, self.candidate, self.base))))
        self.assertEqual(self.service.load()["revision"], 2)

    def test_commit_refuses_a_measurement_that_took_the_graph_meanwhile(self):
        from audio.output_service import MeasurementActiveError
        runtime = make_transition_runtime()
        owned = OutputService(replace(self.service._deps, measurement_active=lambda: True))
        payload = v2_payload(self.service, self.candidate, self.base)
        with mock.patch.object(main, "get_output_service", return_value=owned):
            with self.assertRaises(MeasurementActiveError):
                asyncio.run(runtime.commit_output_mode_runtime(v2_request(payload)))
        self.assertEqual(self.service.load()["revision"], 1)

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
            deps.get_output_service = lambda: main.get_output_service()
            result = asyncio.run(runtime.finalize_output_mode_graph_after_gate_open(request))
        return result, sub

    def test_finalize_skips_legacy_sub_reconcile_for_v2(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(directory)
            service = make_service(directory, manager)
            base = seed_sub_state(service)
            candidate = set_bank_preset(base, "stereo-sub", "sub1", preset="Room")
            target = plan_target(service, manager, candidate)
            request = v2_request(v2_payload(service, candidate, base, target=target))
            service.commit(candidate, expected_revision=1)
            with mock.patch.object(main, "get_output_service", return_value=service):
                result, sub = self.finalize(request)
            self.assertEqual(service.load_applied(
                output_key="A", channels=4, hardware_ports=list(target.config.hardware_ports)), service.load())
        self.assertTrue(result["graph_complete"])
        sub.assert_not_awaited()


class VerifyFingerprintTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)
        self.base = seed_sub_state(self.service)
        self.candidate = set_bank_preset(self.base, "stereo-sub", "sub1", preset="Room")

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
        self.candidate = set_bank_preset(self.base, "stereo-sub", "sub1", preset="Room")
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
        self.assertEqual(self.service.load()["modes"]["stereo-sub"]["banks"]["sub1"]["preset"], "Neutral")
        fake.sync_rendered.assert_awaited_once()
        restored = fake.sync_rendered.await_args.args[0]
        self.assertEqual(restored.config, self.previous_target.config)
        self.assertEqual(restored.text, self.previous_target.text)
        restored.validate_current()

    def test_concurrent_winner_skips_rollback(self):
        self.service.commit(self.candidate, expected_revision=1)
        self.service.apply(lambda state: set_bank_preset(state, "stereo-sub", "global", preset="Room"),
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
        fake.sync_rendered.assert_awaited_once()
        restored = fake.sync_rendered.await_args.args[0]
        self.assertEqual(restored.config, self.previous_target.config)
        self.assertEqual(restored.text, self.previous_target.text)
        restored.validate_current()

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
                "mutation": {"kind": "set_routing", "mode": "stereo-sub",
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
        candidate_roles = payload["candidate_state"]["modes"]["stereo-sub"]["routing"]["A"]
        self.assertIn("sub2", candidate_roles)

    def test_coordinator_failure_leaves_candidate_uncommitted(self):
        async def fail(_request):
            raise PlaybackTransitionFailure("boom", transition_id="t", stage="output-mode-persist")

        with route_context(self.service, self.manager, fail):
            with self.assertRaises(main.HTTPException) as ctx:
                asyncio.run(main.apply_audio_output_state(FakeRequest({
                    "expected_revision": 1,
                    "mutation": {"kind": "set_routing", "mode": "stereo-sub",
                                 "assignments": ["main_l", "main_r", "sub1", "sub2"]},
                })))
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(self.service.load()["revision"], 1)

    def test_failed_post_gate_verification_never_promotes_candidate_checkpoint(self):
        base = self.service.load()
        ports = [f"playback_AUX{i}" for i in range(4)]
        self.service.remember_applied(base, output_key="A", channels=4,
                                     hardware_ports=ports, sample_rate_hz=48000)
        head = self.service.commit(set_crossover(base, "stereo-sub", True), expected_revision=1)
        transition = make_transition_runtime()

        async def fail_after_persist(request):
            await transition.commit_output_mode_runtime(request)
            await transition.rollback_output_mode_runtime(request, {})
            raise PlaybackTransitionFailure("post-gate failure", transition_id="t", stage="post-gate")

        with route_context(self.service, self.manager, fail_after_persist), \
             mock.patch.object(main.runtime, "dsp_runtime", mock.MagicMock()):
            with self.assertRaises(main.HTTPException):
                asyncio.run(main.apply_audio_output_state(FakeRequest({
                    "expected_revision": head["revision"],
                    "mutation": {"kind": "set_crossover", "mode": "stereo-sub", "enabled": False}})))
        self.assertEqual(self.service.load_applied(output_key="A", channels=4, hardware_ports=ports), base)

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
                    "mutation": {"kind": "set_routing", "mode": "stereo-sub",
                                 "assignments": ["main_l", "main_r", "sub1", "sub2"]},
                })))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_coordinator_measurement_owner_maps_to_423(self):
        from audio.output_service import MeasurementActiveError

        async def owned(_request):
            failure = PlaybackTransitionFailure("owned", transition_id="t",
                                                stage="output-mode-persist")
            failure.__cause__ = MeasurementActiveError("Measurement is active; output state is locked")
            raise failure

        with route_context(self.service, self.manager, owned):
            with self.assertRaises(main.HTTPException) as ctx:
                asyncio.run(main.apply_audio_output_state(FakeRequest({
                    "expected_revision": 1,
                    "mutation": {"kind": "set_routing", "mode": "stereo-sub",
                                 "assignments": ["main_l", "main_r", "sub1", "sub2"]},
                })))
        self.assertEqual(ctx.exception.status_code, 423)
        self.assertEqual(self.service.load()["revision"], 1)

    def test_sync_plan_runtime_binding_forwards_target(self):
        fake = mock.MagicMock()
        fake.sync_rendered = AsyncMock()
        with mock.patch.object(main.runtime, "dsp_runtime", fake):
            asyncio.run(main.output_renderer.sync_plan_runtime({"config": "c", "text": "t"}))
        fake.sync_rendered.assert_awaited_once()
        target = fake.sync_rendered.await_args.args[0]
        self.assertEqual((target.config, target.text), ("c", "t"))

    def test_sync_plan_runtime_without_runtime_fails(self):
        with mock.patch.object(main.runtime, "dsp_runtime", None):
            with self.assertRaises(RuntimeError):
                asyncio.run(main.output_renderer.sync_plan_runtime({"config": "c", "text": "t"}))


class StabilizeStagedGraphTests(unittest.IsolatedAsyncioTestCase):
    """Auto Gain/Loudness stabilization must not re-render the previous head.

    The staged v2 graph is live while the store still holds the previous
    state.  The guarded effects re-apply renders that store head, so it
    replaced the staged graph (or the legacy overview when the previous head
    could not activate, e.g. a Surround crossover draft) and the transition
    failed at effects-dsp-stabilize with "production graph changed".
    """

    SURROUND = ["main_l", "main_r", "rear_l", "rear_r", "sub1", "sub2"]

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)
        extras = self.manager.load_global_extras()
        extras["autogain"]["enabled"] = True
        self.manager.save_global_extras(extras)
        self.manager.LOUDNESS_STRENGTH_VOLUME_SETTLE_SECONDS = 0.0
        async def rebuild(*_args, apply_candidate, **_kwargs):
            apply_candidate()

        self.engine = mock.MagicMock(guarded_rebuild=AsyncMock(side_effect=rebuild),
                                     guarded_rebuild_rendered=AsyncMock(side_effect=rebuild),
                                     preflight_rendered=AsyncMock())
        self.runtime = make_transition_runtime()

    def commit_head(self, state):
        return self.service.commit(state, expected_revision=0)

    def surround_state(self):
        return switch_mode(set_mode_routing(
            self.service.load(), "surround", OUTPUT_KEY, self.SURROUND), "surround")

    async def stabilize(self, head, candidate, *, staged=True):
        overview = {
            "selected_output": {"key": OUTPUT_KEY, "channels": 6},
            "output_mode": {"effective_output_key": OUTPUT_KEY, "effective_output_channels": 6,
                            "hardware_playback_ports": list(PORTS)},
        }
        payload = v2_payload(self.service, candidate, head, expected_revision=head["revision"],
                             target=mock.sentinel.target if staged else None)
        loop = asyncio.get_running_loop()
        self.manager.runtime_transition_callback = (
            lambda previous, current, persist: asyncio.run_coroutine_threadsafe(
                 main.output_renderer.guarded_effects_transition(previous, current, persist), loop).result())
        with mock.patch.multiple(
                main,
                get_output_service=mock.MagicMock(return_value=self.service),
                get_audio_output_overview=mock.MagicMock(return_value=overview),
                get_samplerate_status=mock.MagicMock(
                    return_value={"active_rate": 48000, "force_rate": 48000}),
                _require_dsp_manager=mock.MagicMock(return_value=self.manager),
                dsp_manager=self.manager,
                _volume_state_for_manager=AsyncMock(return_value=SimpleNamespace(
                    loudness_in_path=False, preset=None, dsp_guard_db=0.0, master_percent=100))), \
             mock.patch.object(main.runtime, "dsp_runtime", self.engine), \
             mock.patch.object(playback_orchestration.configured(), "playback_graph_diagnosis",
                               new=AsyncMock(return_value={"links_complete": True, "signature": "sig"})), \
             mock.patch.object(self.runtime, "_read_and_validate_effects_runtime",
                               new=AsyncMock(return_value={})) as readback:
            result = await self.runtime.stabilize_effects_after_rate_change(
                v2_request(payload), dsp_reinitialized=True)
        return result, readback

    async def test_crossover_off_keeps_the_staged_surround_graph(self):
        head = self.commit_head(set_crossover(self.surround_state(), "surround", True))
        candidate = set_crossover(head, "surround", False)
        result, readback = await self.stabilize(head, candidate)
        self.assertTrue(result["stabilized"])
        self.assertFalse(result["no_op"])
        self.engine.guarded_rebuild.assert_not_awaited()
        self.engine.guarded_rebuild_rendered.assert_not_awaited()
        readback.assert_awaited_once()

    async def test_activatable_previous_head_is_not_re_rendered_either(self):
        head = self.commit_head(self.surround_state())
        candidate = set_mode_routing(head, "surround", OUTPUT_KEY,
                                     ["main_l", "main_r", "rear_r", "rear_l", "sub1", "sub2"])
        result, _ = await self.stabilize(head, candidate)
        self.assertTrue(result["stabilized"])
        self.engine.guarded_rebuild.assert_not_awaited()
        self.engine.guarded_rebuild_rendered.assert_not_awaited()

    async def test_unstaged_transition_without_recovery_refuses_legacy_rebuild(self):
        head = self.commit_head(set_crossover(self.surround_state(), "surround", True))
        candidate = set_crossover(head, "surround", False)
        self.engine.fail_closed = AsyncMock()
        with self.assertRaises(RuntimeError):
            await self.stabilize(head, candidate, staged=False)
        self.engine.guarded_rebuild.assert_not_awaited()
        self.engine.guarded_rebuild_rendered.assert_not_awaited()

    async def test_unstaged_transition_recovers_last_applied_surround_for_extras(self):
        valid = self.surround_state()
        self.service.remember_applied(valid, output_key=OUTPUT_KEY, channels=6,
                                     hardware_ports=PORTS, sample_rate_hz=48000)
        head = self.commit_head(set_crossover(valid, "surround", True))
        candidate = set_crossover(head, "surround", False)
        result, _ = await self.stabilize(head, candidate, staged=False)
        self.assertTrue(result["stabilized"])
        self.engine.guarded_rebuild.assert_not_awaited()
        target = self.engine.guarded_rebuild_rendered.await_args.args[0]
        self.assertEqual(target.config.output_mode, "surround")
        self.assertEqual(target.config.input_count, 8)


class LegacyFilesUntouchedTests(unittest.TestCase):
    """Task-2 contract: v2 commit/rollback never touch legacy files.

    Adaptation of the plan sketch to this file's harness: the topology
    switch is the CommitV2/RollbackV2 runtime path (the v2 route builds
    requests with only ``output_state_transition`` and never sets the
    legacy ``output_mode_config``/``output_routing_config`` fields, so the
    runtime-level ``output_mode_persisted`` assertion is the equivalent of
    the sketch's ``live_applied``). Legacy writers raise; pre-existing
    sentinel legacy files must be byte-identical afterwards (canary).
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        xdg = tempfile.TemporaryDirectory()
        self.addCleanup(xdg.cleanup)
        self.manager = make_manager(directory.name)
        self.service = make_service(directory.name, self.manager)
        self.base = seed_sub_state(self.service)
        self.candidate = set_bank_preset(self.base, "stereo-sub", "sub1", preset="Room")
        self.previous_target = plan_target(self.service, self.manager, self.base)
        self.xdg_patch = mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": xdg.name})
        self.xdg_patch.start()
        self.addCleanup(self.xdg_patch.stop)
        self.mode_path = Path(xdg.name) / "fxroute" / "audio-output-mode.json"
        self.routing_path = Path(xdg.name) / "fxroute" / "output-routing.json"
        self.mode_path.parent.mkdir(parents=True, exist_ok=True)
        self.mode_path.write_bytes(b'{"mode": "sentinel"}')
        self.routing_path.write_bytes(b'{"sentinel": [1, 2, 3, 4]}')

    def legacy_absent(self):
        """Structural pin: no legacy persist/restore surface remains."""
        import audio.samplerate as samplerate_module
        from audio import output_routing as routing_module
        import playback.runtime.output_mode as runtime_output_mode
        import playback.runtime.snapshot as runtime_snapshot
        for module, names in (
            (samplerate_module, ("persist_audio_output_mode",
                                 "set_audio_output_mode")),
            (routing_module, ("save_assignments", "restore_routing_state",
                              "saved_routing_state")),
            (runtime_output_mode, ("persist_audio_output_mode",
                                   "save_assignments",
                                   "restore_routing_state")),
            (runtime_snapshot, ("saved_routing_state",)),
        ):
            for name in names:
                self.assertFalse(hasattr(module, name),
                                 f"{module.__name__}.{name}")
        source = inspect.getsource(runtime_output_mode) + inspect.getsource(runtime_snapshot)
        for token in ("persist_audio_output_mode", "save_assignments",
                      "restore_routing_state", "saved_routing_state",
                      "output_mode_config", "output_routing_config"):
            self.assertNotIn(token, source)

    def legacy_bytes(self):
        return (self.mode_path.read_bytes(), self.routing_path.read_bytes())

    def test_topology_switch_never_touches_legacy_files(self):
        before = self.legacy_bytes()
        self.legacy_absent()
        runtime = make_transition_runtime()
        with mock.patch.object(main, "get_output_service", return_value=self.service):
            result = asyncio.run(runtime.commit_output_mode_runtime(
                v2_request(v2_payload(self.service, self.candidate, self.base))))
        self.assertTrue(result["output_mode_persisted"])
        self.assertEqual(self.service.load()["revision"], 2)
        self.assertEqual(self.legacy_bytes(), before)

    def test_rollback_never_touches_legacy_files(self):
        self.service.commit(self.candidate, expected_revision=1)
        before = self.legacy_bytes()
        self.legacy_absent()
        runtime = make_transition_runtime()
        fake = mock.MagicMock()
        fake.sync_rendered = AsyncMock()
        with mock.patch.object(main, "get_output_service", return_value=self.service), \
                mock.patch.object(main.runtime, "dsp_runtime", fake), \
                mock.patch.object(playback_orchestration.configured(), "playback_graph_diagnosis",
                                  new=AsyncMock(return_value={"links_complete": True,
                                                              "signature": "sig"})):
            asyncio.run(runtime.rollback_output_mode_runtime(
                v2_request(v2_payload(self.service, self.candidate, self.base,
                                      previous_target=self.previous_target)),
                snapshot={}))
        self.assertEqual(self.service.load()["revision"], 3)
        fake.sync_rendered.assert_awaited_once()
        restored = fake.sync_rendered.await_args.args[0]
        self.assertEqual(restored.config, self.previous_target.config)
        self.assertEqual(restored.text, self.previous_target.text)
        restored.validate_current()
        self.assertEqual(self.legacy_bytes(), before)


if __name__ == "__main__":
    unittest.main()
