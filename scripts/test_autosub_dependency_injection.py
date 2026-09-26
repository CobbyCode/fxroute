#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""AutoSub resolves all application dependencies through injection.

Proves the measurement.autosub package no longer imports main.py: the native DSP runtime, the
measurement store, the measurement sample-rate session and the DSP manager
are all read through the injected ``AutoSubDependencies`` accessors.  This
module imports only ``autosub`` (plus the stdlib); it never imports ``main``,
and ``autosub`` itself must not pull ``main`` into ``sys.modules``.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import measurement.autosub as autosub
import measurement.autosub.candidates as autosub_candidates
import measurement.autosub.deps as autosub_deps
import measurement.autosub.jobs as autosub_jobs


def overview_21(alignment=2.34, mode="subwoofer-2.1", fc=80, level=-3.0,
                polarity="normal", highpass=True):
    return {
        "selected_output": {"key": "mock", "channels": 4,
                            "active_rate": 48000, "label": "Mock"},
        "output_mode": {
            "mode": mode,
            "crossover_frequency_hz": fc,
            "main_highpass_enabled": highpass,
            "subwoofer": {
                "crossover_frequency_hz": fc,
                "main_highpass_enabled": highpass,
                "sub_alignment_ms": alignment,
                "sub_level_db": level,
                "sub_polarity": polarity,
            },
        },
    }


class FakeDSPRuntime:
    def __init__(self):
        self.sync = AsyncMock()

    def snapshot(self):
        return {"active": True}


class FakeStore:
    def __init__(self):
        self.cancelled = []

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)


class FakeSession:
    def __init__(self):
        self.unregister_auto_sub = AsyncMock()


def _configure(*, dsp_runtime=None, store=None, session=None, manager=None):
    autosub.configure_dependencies(autosub.AutoSubDependencies(
        get_dsp_runtime=lambda: dsp_runtime,
        get_measurement_store=lambda: store,
        get_measurement_session=lambda: session,
        get_dsp_manager=lambda: manager,
    ))


class AutoSubDependencyInjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # autosub_deps._autosub_deps is process-global: snapshot it before
        # each test configures fakes and restore it afterwards, so later
        # test modules in the same run keep the configuration they expect.
        self.previous_dependencies = autosub_deps._autosub_deps
        self.addCleanup(setattr, autosub_deps, "_autosub_deps", self.previous_dependencies)

    async def asyncTearDown(self):
        autosub_deps._AUTO_SUB_JOBS.clear()
        autosub_deps._AUTO_SUB_WORKER_TASKS.clear()
        for task in list(autosub_deps._AUTO_SUB_CLEANUP_TASKS):
            task.cancel()
        autosub_deps._AUTO_SUB_CLEANUP_TASKS.clear()
        try:
            autosub_deps._auto_sub_lock.release()
        except RuntimeError:
            pass

    def test_autosub_import_does_not_import_main(self):
        self.assertNotIn("main", sys.modules)

    # NOTE (backend-v2 migration): the three _auto_sub_sync_dsp_runtime tests
    # pinned the deleted legacy persist-then-sync helper. Late-bound
    # dependency injection itself is still covered by the service suites
    # configuring AutoSubDependencies (owner prearm, service start, runner IO).

    async def test_shutdown_uses_injected_measurement_store(self):
        store = FakeStore()
        _configure(store=store)
        autosub_deps._AUTO_SUB_JOBS["j1"] = {
            "id": "j1",
            "status": "running",
            "cancel_requested": False,
            "current_sweep_id": "sweep-1",
        }
        await autosub.shutdown()
        self.assertEqual(store.cancelled, ["sweep-1"])

    def test_playback_gain_uses_injected_dsp_manager(self):
        manager = SimpleNamespace(load_global_extras=lambda: {
            "loudness": {"enabled": True, "params": {"volumeDb": -20.0}},
        })
        _configure(manager=manager)
        captured = autosub_jobs._capture_auto_sub_playback_gain()
        self.assertEqual(captured["volume_db"], -20.0)
        self.assertEqual(captured["source"], "loudness.params.volumeDb")

    async def test_finish_worker_uses_injected_measurement_session(self):
        session = FakeSession()
        _configure(session=session)
        await autosub_deps._auto_sub_lock.acquire()
        job = {"id": "j1", "status": "failed", "cancel_requested": False}
        await autosub_jobs._finish_auto_sub_worker(job, "j1")
        session.unregister_auto_sub.assert_awaited_once_with("j1")


class CandidateDependencyInjectionTests(unittest.TestCase):
    def setUp(self):
        self.previous_dependencies = autosub_deps._autosub_deps
        self.addCleanup(setattr, autosub_deps, "_autosub_deps", self.previous_dependencies)

    def configure_candidate_dependencies(self, service_accessor, factory):
        autosub.configure_dependencies(autosub.AutoSubDependencies(
            get_dsp_runtime=lambda: None,
            get_measurement_store=lambda: None,
            get_measurement_session=lambda: None,
            get_dsp_manager=lambda: None,
            get_output_service=service_accessor,
            create_candidate_session=factory))

    def test_output_service_accessor_is_late_bound(self):
        self.assertTrue(hasattr(autosub_deps, "_output_service"))
        services = [object(), object()]
        current = [services[0]]
        self.configure_candidate_dependencies(lambda: current[0], lambda **kwargs: kwargs)
        self.assertIs(autosub_deps._output_service(), services[0])
        current[0] = services[1]
        self.assertIs(autosub_deps._output_service(), services[1])
        self.configure_candidate_dependencies(lambda: services[0], lambda **kwargs: kwargs)
        self.assertIs(autosub_deps._output_service(), services[0])

    def test_candidate_factory_is_late_bound_passthrough(self):
        self.assertTrue(hasattr(autosub_deps, "create_candidate_session"))
        first, second = object(), object()
        received = []

        def factory(*args, **kwargs):
            received.append((args, kwargs))
            return second

        self.configure_candidate_dependencies(lambda: None, lambda **kwargs: first)
        self.assertIs(autosub_deps.create_candidate_session(start_state={}), first)
        self.configure_candidate_dependencies(lambda: None, factory)
        state = {"revision": 7}
        self.assertIs(autosub_deps.create_candidate_session("job", start_state=state), second)
        self.assertEqual(received, [(("job",), {"start_state": state})])
        self.assertIs(received[0][1]["start_state"], state)

    def test_unconfigured_candidate_dependencies_fail_closed(self):
        self.assertTrue(hasattr(autosub_deps, "_output_service"))
        self.assertTrue(hasattr(autosub_deps, "create_candidate_session"))
        for configured in (False, True):
            if configured:
                _configure()
            else:
                autosub_deps._autosub_deps = None
            with self.subTest(configured=configured):
                with self.assertRaises(RuntimeError):
                    autosub_deps._output_service()
                with self.assertRaises(RuntimeError):
                    autosub_deps.create_candidate_session(start_state={})

    def test_owner_registry_is_separate_and_has_no_missing_owner_fallback(self):
        self.assertTrue(hasattr(autosub_deps, "_AUTO_SUB_CANDIDATE_OWNERS"))
        self.addCleanup(autosub_deps._AUTO_SUB_CANDIDATE_OWNERS.clear)
        self.addCleanup(autosub_deps._AUTO_SUB_JOBS.pop, "owner-test", None)
        job = {"id": "owner-test", "status": "running"}
        autosub_deps._AUTO_SUB_JOBS["owner-test"] = job
        owner = object()
        with self.assertRaises(RuntimeError):
            autosub_deps._candidate_owner("owner-test")
        autosub_deps.register_candidate_owner("owner-test", owner)
        self.assertIs(autosub_deps._candidate_owner("owner-test"), owner)
        self.assertEqual(job, {"id": "owner-test", "status": "running"})
        autosub_deps.drop_candidate_owner("owner-test")
        autosub_deps.drop_candidate_owner("owner-test")
        with self.assertRaises(RuntimeError):
            autosub_deps._candidate_owner("owner-test")


if __name__ == "__main__":
    unittest.main(verbosity=2)
