#!/usr/bin/env python3
"""AutoSub resolves all application dependencies through injection.

Proves measurement/autosub.py no longer imports main.py: the native DSP runtime, the
measurement store, the measurement sample-rate session and the DSP manager
are all read through the injected ``AutoSubDependencies`` accessors.  This
module imports only ``autosub`` (plus the stdlib); it never imports ``main``,
and ``autosub`` itself must not pull ``main`` into ``sys.modules``.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import measurement.autosub as autosub


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
    async def asyncTearDown(self):
        autosub._AUTO_SUB_JOBS.clear()
        autosub._AUTO_SUB_WORKER_TASKS.clear()
        for task in list(autosub._AUTO_SUB_CLEANUP_TASKS):
            task.cancel()
        autosub._AUTO_SUB_CLEANUP_TASKS.clear()
        try:
            autosub._auto_sub_lock.release()
        except RuntimeError:
            pass

    def test_autosub_import_does_not_import_main(self):
        self.assertNotIn("main", sys.modules)

    async def test_sync_uses_injected_dsp_runtime(self):
        runtime = FakeDSPRuntime()
        _configure(dsp_runtime=runtime)
        persisted = overview_21()
        with patch.object(autosub.candidates, "get_audio_output_overview", return_value=overview_21()):
            await autosub._auto_sub_sync_dsp_runtime(
                output_mode="subwoofer-2.1", persisted_overview=persisted)
        runtime.sync.assert_awaited_once()

    async def test_late_bound_accessor_observes_reconfiguration(self):
        first = FakeDSPRuntime()
        replacement = FakeDSPRuntime()
        _configure(dsp_runtime=first)
        _configure(dsp_runtime=replacement)
        persisted = overview_21()
        with patch.object(autosub.candidates, "get_audio_output_overview", return_value=overview_21()):
            await autosub._auto_sub_sync_dsp_runtime(
                output_mode="subwoofer-2.1", persisted_overview=persisted)
        replacement.sync.assert_awaited_once()
        first.sync.assert_not_awaited()

    async def test_none_dsp_runtime_is_a_noop(self):
        _configure(dsp_runtime=None)
        with patch.object(autosub.candidates, "get_audio_output_overview", return_value=overview_21()):
            await autosub._auto_sub_sync_dsp_runtime(
                output_mode="subwoofer-2.1", persisted_overview=overview_21())

    async def test_shutdown_uses_injected_measurement_store(self):
        store = FakeStore()
        _configure(store=store)
        autosub._AUTO_SUB_JOBS["j1"] = {
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
        captured = autosub._capture_auto_sub_playback_gain()
        self.assertEqual(captured["volume_db"], -20.0)
        self.assertEqual(captured["source"], "loudness.params.volumeDb")

    async def test_finish_worker_uses_injected_measurement_session(self):
        session = FakeSession()
        _configure(session=session)
        await autosub._auto_sub_lock.acquire()
        job = {"id": "j1", "status": "failed", "cancel_requested": False}
        await autosub._finish_auto_sub_worker(job, "j1")
        session.unregister_auto_sub.assert_awaited_once_with("j1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
