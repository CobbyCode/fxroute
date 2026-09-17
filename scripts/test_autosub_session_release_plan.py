#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Slice G: session release rebuilds committed plans, not the legacy overview.

After an AutoSub service run commits its winner, the persisted legacy mode
file still shows the frozen start (service jobs never persist legacy).  A
session release that re-syncs the runtime from the legacy overview would
therefore rebuild the stale graph and audibly lose the committed winner.
Instead:

* the finalizer registers a release-runtime adapter on the measurement
  session before unregistering a committed service job; the adapter renders
  the current ``OutputService.load()`` at the restore rate,
* the release invokes the adapter at the measurement-only boundary instead
  of the legacy overview sync, and the playback-coordinator release consumes
  the committed plan too,
* the adapter is cleared when the release completes; without an adapter (or
  without a commit) the legacy path stays byte-identical.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audio.output_service import OutputService, OutputServiceDeps  # noqa: E402
from audio.output_state import default_output_state, set_mode_routing  # noqa: E402
from audio.output_state_store import OutputStateStore  # noqa: E402
from dsp.manager import DSPManager  # noqa: E402

import measurement.session as measurement_session  # noqa: E402
from measurement.session import (  # noqa: E402
    MeasurementSampleRateSession,
    MeasurementServices,
)
from measurement.autosub import deps as autosub_deps  # noqa: E402
from measurement.autosub import jobs as autosub_jobs  # noqa: E402
from measurement.autosub import release as autosub_release  # noqa: E402

RATE = 48000
RESTORE_RATE = 44100
PORTS = [f"playback_AUX{i}" for i in range(4)]


class ReleaseServices:
    """Fake measurement services with an unreadable legacy overview."""

    def __init__(self):
        self.sync_at_rate_calls = []
        self.coordinator_restores = []
        self.coordinator_committed = True
        self.end_attempts = 0
        self.track_info = None
        self.intent_current = True

    def fail_overview(self, *args, **kwargs):
        raise AssertionError("legacy overview read during committed-plan release")

    async def sync_runtime_at_rate(self, rate, **kwargs):
        self.sync_at_rate_calls.append(rate)

    async def restore_measurement(self, **kwargs):
        self.coordinator_restores.append(kwargs)
        return SimpleNamespace(committed=self.coordinator_committed)

    async def intent_matches(self, **kwargs):
        return self.intent_current

    def build(self):
        return MeasurementServices(
            get_store=lambda: None,
            get_session=lambda: None,
            auto_sub_active=lambda: False,
            get_dsp_runtime=lambda: None,
            get_player=lambda: None,
            get_samplerate_status=lambda: {
                "clock_rate": RESTORE_RATE, "active_rate": RESTORE_RATE,
                "force_rate": RESTORE_RATE},
            get_audio_output_overview=self.fail_overview,
            get_current_track_info=lambda: self.track_info,
            get_playback_transition_coordinator=lambda: SimpleNamespace(
                restore_measurement=self.restore_measurement),
            get_dsp_orchestrator=lambda: SimpleNamespace(
                sync_runtime_at_rate=self.sync_runtime_at_rate),
            get_playback_intent_generation=lambda: 0,
            run_coordinated_transition=lambda *a, **k: None,
            coordinator_current_playback_context=lambda: None,
            begin_playback_transition_attempt=lambda: 7,
            end_playback_transition_attempt=self.end_attempt,
            get_current_pipewire_force_rate=lambda: RESTORE_RATE,
            set_pipewire_force_rate=lambda *a, **k: None,
            ensure_playback_samplerate_force=lambda *a, **k: None,
            wait_for_samplerate_alignment=lambda *a, **k: None,
            reconcile_transition_sink_rate=lambda *a, **k: None,
            playback_graph_diagnosis=lambda *a, **k: None,
            log_playback_graph_diagnosis=lambda *a, **k: None,
            measurement_restore_intent_matches_live_state=self.intent_matches,
            spotify_snapshot_identity_values=lambda snapshot: set(),
            spotify_target_track_from_state=lambda *a, **k: {},
            get_player_audio_samplerate=lambda *a, **k: None,
            pulse_suspend_sink_for_samplerate=lambda *a, **k: None,
            audio_output_overview_with_effective_rate=lambda *a, **k: {},
            spotify_prearm_sample_rate_hz=44100,
            pipewire_handoff_poll_interval_ms=50,
        )

    def end_attempt(self):
        self.end_attempts += 1


def drive_release(session, job_id="release-job"):
    """Simulate an AutoSub-held window closing: register, close, unregister."""
    session.active = True
    session.measurement_rate = RATE
    session.active_auto_sub_job_id = job_id
    return job_id


class SessionReleasePlanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fakes = ReleaseServices()
        measurement_session.configure_services(self.fakes.build())
        self.addCleanup(setattr, measurement_session, "_services", None)
        self.addCleanup(setattr, measurement_session,
                        "_playback_state_before_measurement", None)
        measurement_session._playback_state_before_measurement = None
        self.session = MeasurementSampleRateSession()
        self.adapter_rates = []

        async def adapter(restore_rate_hz):
            self.adapter_rates.append(restore_rate_hz)
            return {"revision": 2, "plan_fingerprint": "fp-test",
                    "sample_rate_hz": restore_rate_hz}

        self.adapter = adapter

    async def release(self, job_id="release-job"):
        drive_release(self.session, job_id)
        await self.session.request_close()
        await self.session.register_autosub_release_adapter(self.adapter)
        await self.session.unregister_auto_sub(job_id)

    async def test_measurement_only_release_invokes_adapter_instead_of_legacy_sync(self):
        await self.release()
        self.assertEqual(self.adapter_rates, [RESTORE_RATE])
        self.assertEqual(self.fakes.sync_at_rate_calls, [])
        self.assertEqual(self.fakes.coordinator_restores, [])

    async def test_release_without_adapter_keeps_legacy_sync(self):
        drive_release(self.session)
        await self.session.request_close()
        await self.session.unregister_auto_sub("release-job")
        self.assertEqual(self.adapter_rates, [])
        self.assertEqual(self.fakes.sync_at_rate_calls, [RESTORE_RATE])

    async def test_coordinator_release_also_consumes_committed_plan(self):
        self.fakes.track_info = {"source": "local"}
        measurement_session._playback_state_before_measurement = {
            "source": "local", "expected_rate": RESTORE_RATE,
            "was_playing": True, "url": "file:///test.flac",
            "path": "/test.flac", "id": "t-1", "title": "Test",
            "position": 10.0, "intent_generation": 0,
        }
        await self.release()
        self.assertEqual(len(self.fakes.coordinator_restores), 1)
        self.assertEqual(self.adapter_rates, [RESTORE_RATE])
        self.assertEqual(self.fakes.sync_at_rate_calls, [])

    async def test_adapter_cleared_on_completed_release(self):
        await self.release()
        self.assertEqual(self.adapter_rates, [RESTORE_RATE])
        self.assertIsNone(self.session._autosub_release_adapter)
        # A later release without re-registration falls back to legacy sync.
        drive_release(self.session, "release-job-2")
        await self.session.request_close()
        await self.session.unregister_auto_sub("release-job-2")
        self.assertEqual(self.adapter_rates, [RESTORE_RATE])
        self.assertEqual(self.fakes.sync_at_rate_calls, [RESTORE_RATE])

    async def test_register_adapter_rejects_non_callable(self):
        with self.assertRaises(ValueError):
            await self.session.register_autosub_release_adapter(None)

    async def test_refusing_adapter_fails_closed_without_wedging_release(self):
        async def refusing_adapter(restore_rate_hz):
            raise RuntimeError("device switched since commit")

        drive_release(self.session)
        await self.session.request_close()
        await self.session.register_autosub_release_adapter(refusing_adapter)
        await self.session.unregister_auto_sub("release-job")
        self.assertIsNone(self.session._autosub_release_adapter)
        self.assertEqual(self.fakes.sync_at_rate_calls, [])

    async def test_services_default_to_no_release_factory(self):
        fields = {name: (lambda: None) for name in (
            "get_store", "get_session", "get_dsp_runtime", "get_player",
            "get_samplerate_status", "get_audio_output_overview",
            "get_current_track_info", "get_playback_transition_coordinator",
            "get_dsp_orchestrator")}
        services = MeasurementServices(
            auto_sub_active=lambda: False,
            get_playback_intent_generation=lambda: 0,
            run_coordinated_transition=lambda *a, **k: None,
            coordinator_current_playback_context=lambda: None,
            begin_playback_transition_attempt=lambda: 0,
            end_playback_transition_attempt=lambda: None,
            get_current_pipewire_force_rate=lambda: None,
            set_pipewire_force_rate=lambda *a, **k: None,
            ensure_playback_samplerate_force=lambda *a, **k: None,
            wait_for_samplerate_alignment=lambda *a, **k: None,
            reconcile_transition_sink_rate=lambda *a, **k: None,
            playback_graph_diagnosis=lambda *a, **k: None,
            log_playback_graph_diagnosis=lambda *a, **k: None,
            measurement_restore_intent_matches_live_state=lambda *a, **k: None,
            spotify_snapshot_identity_values=lambda *a, **k: set(),
            spotify_target_track_from_state=lambda *a, **k: {},
            get_player_audio_samplerate=lambda *a, **k: None,
            pulse_suspend_sink_for_samplerate=lambda *a, **k: None,
            audio_output_overview_with_effective_rate=lambda *a, **k: {},
            spotify_prearm_sample_rate_hz=44100,
            pipewire_handoff_poll_interval_ms=50,
            **fields)
        self.assertIsNone(services.build_autosub_release_adapter)


class ReleaseAdapterRenderTests(unittest.IsolatedAsyncioTestCase):
    """The adapter renders the current committed load at the restore rate."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="autosub-release-")
        self.addCleanup(directory.cleanup)
        self.manager = DSPManager(home=Path(directory.name) / "dsp")
        self.store = OutputStateStore(Path(directory.name) / "output-state.json")
        self.service = OutputService(OutputServiceDeps(
            store=self.store, preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: True))
        state = set_mode_routing(default_output_state(), "stereo", "dev",
                                 ["main_l", "main_r", "sub1"])
        state["modes"]["stereo"]["processing"]["sub1"].update(
            alignment_ms=2.0, level_db=-3.0)
        self.start = self.service.commit(state, expected_revision=0)
        self.targets = []

        class FakeRuntime:
            async def sync_rendered(inner_self, target):
                self.targets.append(target)

        self.runtime = FakeRuntime()

    def build_adapter(self):
        return autosub_release.create_release_adapter(
            service=self.service, dsp_manager=self.manager,
            hardware_ports=list(PORTS),
            get_native_runtime=lambda: self.runtime,
            output_key="dev", channels=4)

    def expected_fingerprint(self, rate):
        plan = self.service.compile_plan(
            self.service.load(), output_key="dev", channels=4,
            sample_rate_hz=rate)
        return self.service.fingerprint_plan(plan)

    async def test_adapter_renders_current_load_at_restore_rate(self):
        adapter = self.build_adapter()
        result = await adapter(RESTORE_RATE)
        self.assertEqual(result["revision"], self.start["revision"])
        self.assertEqual(result["sample_rate_hz"], RESTORE_RATE)
        self.assertEqual(len(self.targets), 1)
        target = self.targets[0]
        self.assertEqual(target.config.sample_rate, RESTORE_RATE)
        self.assertEqual(target.config.plan_fingerprint,
                         self.expected_fingerprint(RESTORE_RATE))
        self.assertEqual(result["plan_fingerprint"],
                         self.expected_fingerprint(RESTORE_RATE))

    async def test_adapter_renders_current_load_after_a_later_commit(self):
        adapter = self.build_adapter()
        first = await adapter(RESTORE_RATE)
        winner = self.service.load()
        winner["modes"]["stereo"]["processing"]["sub1"]["alignment_ms"] = 9.0
        committed = self.service.commit(winner, expected_revision=winner["revision"])
        second = await adapter(RESTORE_RATE)
        self.assertEqual(second["revision"], committed["revision"])
        self.assertNotEqual(second["plan_fingerprint"], first["plan_fingerprint"])
        self.assertEqual(second["plan_fingerprint"],
                         self.expected_fingerprint(RESTORE_RATE))

    async def test_adapter_validates_rate_context_and_runtime(self):
        adapter = self.build_adapter()
        for bad_rate in (0, -1, True, RESTORE_RATE * 1.0, "44100", None):
            with self.subTest(rate=bad_rate), self.assertRaises(ValueError):
                await adapter(bad_rate)
        for kwargs in ({"channels": 0}, {"channels": 4.0},
                       {"output_key": ""}, {"output_key": None},
                       {"hardware_ports": ["playback_AUX0"]}):
            with self.subTest(kwargs=kwargs):
                args = dict(service=self.service, dsp_manager=self.manager,
                            hardware_ports=list(PORTS),
                            get_native_runtime=lambda: self.runtime,
                            output_key="dev", channels=4)
                args.update(kwargs)
                with self.assertRaises(ValueError):
                    autosub_release.create_release_adapter(**args)
        adapter = autosub_release.create_release_adapter(
            service=self.service, dsp_manager=self.manager,
            hardware_ports=list(PORTS),
            get_native_runtime=lambda: None,
            output_key="dev", channels=4)
        with self.assertRaises(RuntimeError):
            await adapter(RESTORE_RATE)
        self.assertEqual(self.targets, [])


class FinalizerRegistrationTests(unittest.IsolatedAsyncioTestCase):
    """The finalizer registers the adapter before unregister (committed only)."""

    def setUp(self):
        self.events = []
        self.lock = asyncio.Lock()
        self.factory_kwargs = []
        session = SimpleNamespace()

        async def register_autosub_release_adapter(adapter):
            self.events.append("registered")

        async def unregister_auto_sub(job_id):
            self.events.append("unregistered")

        session.register_autosub_release_adapter = register_autosub_release_adapter
        session.unregister_auto_sub = unregister_auto_sub
        self.session = session
        self.patches = [
            patch.object(autosub_jobs, "_auto_sub_lock", self.lock),
            patch.object(autosub_jobs, "_measurement_session", lambda: self.session),
            patch.object(autosub_jobs, "_measurement_store", lambda: None),
            patch.object(autosub_jobs, "drop_candidate_owner",
                         lambda job_id: self.events.append("dropped")),
            patch.object(autosub_jobs, "_finalize_autosub_job",
                         lambda job, job_id: self.events.append("finalized")),
            patch.object(autosub_jobs, "_restore_original_config_or_fail_job",
                         self.fake_restore),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(setattr, autosub_deps, "_autosub_deps", None)
        self.addCleanup(setattr, measurement_session, "_services", None)
        self.job_id = "release-job"

    async def fake_restore(self, job, snapshot, message):
        self.events.append("restore")
        return True

    def configure_factory(self, factory):
        measurement_session.configure_services(SimpleNamespace(
            build_autosub_release_adapter=factory))

    def committed_job(self):
        return {"id": self.job_id, "status": "completed",
                "cancel_requested": False,
                "output_state_context": {
                    "mode": "stereo", "revision": 2, "output_key": "dev",
                    "channels": 4, "optimizer_path": "single-sub",
                    "sub_role_map": {"sub1": "sub1"}, "sub_mute_mask": 4,
                    "committed_revision": 2}}

    async def run_cleanup(self, job):
        await self.lock.acquire()
        await autosub_jobs._finish_auto_sub_worker_cleanup(job, self.job_id)

    async def test_committed_service_job_registers_adapter_before_unregister(self):
        def factory(*, output_key, channels):
            self.factory_kwargs.append((output_key, channels))
            return SimpleNamespace()

        self.configure_factory(factory)
        await self.run_cleanup(self.committed_job())
        self.assertEqual(
            self.events,
            ["restore", "registered", "unregistered", "dropped", "finalized"])
        self.assertEqual(self.factory_kwargs, [("dev", 4)])
        self.assertFalse(self.lock.locked())

    async def test_legacy_job_registers_no_adapter(self):
        self.configure_factory(lambda **kwargs: self.fail("factory called"))
        await self.run_cleanup({"id": self.job_id, "status": "completed"})
        self.assertNotIn("registered", self.events)
        self.assertIn("unregistered", self.events)
        self.assertFalse(self.lock.locked())

    async def test_uncommitted_service_job_registers_no_adapter(self):
        self.configure_factory(lambda **kwargs: self.fail("factory called"))
        job = self.committed_job()
        del job["output_state_context"]["committed_revision"]
        await self.run_cleanup(job)
        self.assertNotIn("registered", self.events)
        self.assertIn("unregistered", self.events)
        self.assertFalse(self.lock.locked())

    async def test_missing_factory_or_services_skips_without_failing(self):
        self.configure_factory(None)
        await self.run_cleanup(self.committed_job())
        self.assertNotIn("registered", self.events)
        self.assertIn("unregistered", self.events)
        self.assertFalse(self.lock.locked())
        self.events.clear()
        measurement_session._services = None
        await self.lock.acquire()
        await autosub_jobs._finish_auto_sub_worker_cleanup(
            self.committed_job(), self.job_id)
        self.assertNotIn("registered", self.events)
        self.assertIn("unregistered", self.events)
        self.assertFalse(self.lock.locked())


class ReleaseAdapterLiveDeviceTests(unittest.IsolatedAsyncioTestCase):
    """An orphaned adapter refuses a switched device instead of rebuilding it."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="autosub-release-")
        self.addCleanup(directory.cleanup)
        self.manager = DSPManager(home=Path(directory.name) / "dsp")
        self.store = OutputStateStore(Path(directory.name) / "output-state.json")
        self.service = OutputService(OutputServiceDeps(
            store=self.store, preset_loader=self.manager.preset_store.read,
            resolve_ir=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            measurement_active=lambda: True))
        state = set_mode_routing(default_output_state(), "stereo", "dev",
                                 ["main_l", "main_r", "sub1"])
        self.service.commit(state, expected_revision=0)
        self.targets = []

        class FakeRuntime:
            async def sync_rendered(inner_self, target):
                self.targets.append(target)

        self.runtime = FakeRuntime()
        self.live = {"output_key": "dev", "channels": 4,
                     "hardware_ports": list(PORTS)}

    def build_adapter(self, **kwargs):
        args = dict(service=self.service, dsp_manager=self.manager,
                    hardware_ports=list(PORTS),
                    get_native_runtime=lambda: self.runtime,
                    output_key="dev", channels=4,
                    resolve_live_device=lambda: self.live)
        args.update(kwargs)
        return autosub_release.create_release_adapter(**args)

    async def test_matching_live_device_renders(self):
        result = await self.build_adapter()(RESTORE_RATE)
        self.assertEqual(result["sample_rate_hz"], RESTORE_RATE)
        self.assertEqual(len(self.targets), 1)

    async def test_switched_device_refuses_before_touching_runtime(self):
        adapter = self.build_adapter()
        self.live = {"output_key": "other-dev", "channels": 4,
                     "hardware_ports": list(PORTS)}
        with self.assertRaisesRegex(RuntimeError, "output_key"):
            await adapter(RESTORE_RATE)
        self.assertEqual(self.targets, [])

    async def test_unresolvable_live_device_refuses(self):
        adapter = self.build_adapter()
        self.live = None
        with self.assertRaises(RuntimeError):
            await adapter(RESTORE_RATE)
        self.assertEqual(self.targets, [])

    async def test_non_callable_resolver_rejected_eagerly(self):
        with self.assertRaises(ValueError):
            self.build_adapter(resolve_live_device="dev")

    async def test_without_resolver_keeps_pinned_behavior(self):
        adapter = autosub_release.create_release_adapter(
            service=self.service, dsp_manager=self.manager,
            hardware_ports=list(PORTS),
            get_native_runtime=lambda: self.runtime,
            output_key="dev", channels=4)
        result = await adapter(RESTORE_RATE)
        self.assertEqual(result["sample_rate_hz"], RESTORE_RATE)
        self.assertEqual(len(self.targets), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
