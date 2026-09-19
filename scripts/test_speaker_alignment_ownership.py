#!/usr/bin/env python3
"""Speaker ownership spans both capture passes, commit and rollback cleanup."""
import asyncio
from contextlib import asynccontextmanager
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_measurement_speaker_service import ServiceFixture
from measurement.session import MeasurementSampleRateSession


class SpeakerOwnershipTests(ServiceFixture, unittest.IsolatedAsyncioTestCase):
    async def test_scope_spans_all_captures_and_is_released_before_terminal(self):
        held = []
        original = self.acquire

        @asynccontextmanager
        async def scope(job_id):
            held.append(job_id)
            try:
                yield
            finally:
                await asyncio.sleep(0)
                held.remove(job_id)

        async def capture(alignment, **kwargs):
            self.assertEqual(len(held), 1)
            return await original(alignment, **kwargs)

        service, job_id = self.start(self.service(job_scope=scope, acquire=capture), reference_input_channel="")
        result = await service.wait_for(job_id)
        self.assertEqual(result["status"], "committed", result)
        self.assertEqual(held, [])
        self.assertFalse(service.active)

    async def test_cancel_before_worker_runs_releases_slot(self):
        service, job_id = self.start()
        service.cancel(job_id)
        result = await service.wait_for(job_id, timeout_seconds=1)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(service.active)
        self.assertEqual(self.acquire_calls, [])

    async def test_confirmation_cannot_use_a_different_microphone_chain(self):
        original = self.acquire

        async def capture(alignment, **kwargs):
            result = await original(alignment, **kwargs)
            result["provenance"]["microphone_node"] = f"mic-{len(self.acquire_calls)}"
            return result

        service, job_id = self.start(self.service(acquire=capture))
        result = await service.wait_for(job_id)
        self.assertEqual(result["status"], "failed")
        self.assertIn("input chain", result["error"])
        self.assertFalse(self.session.committed)


class SessionExclusionTests(unittest.IsolatedAsyncioTestCase):
    async def test_speaker_excludes_other_graph_owners_between_way_captures(self):
        session = MeasurementSampleRateSession()
        session.active = True
        await session.register_speaker_job("align")
        self.assertTrue(session.has_active_jobs)
        for register in (session.register_manual_job, session.register_auto_sub, session.register_spl_job):
            with self.assertRaisesRegex(RuntimeError, "Speaker"):
                await register("other")
        await session.unregister_speaker_job("align")
        self.assertFalse(session.has_active_jobs)

    async def test_speaker_cannot_take_over_an_existing_measurement(self):
        session = MeasurementSampleRateSession()
        session.active = True
        await session.register_manual_job("manual")
        with self.assertRaisesRegex(RuntimeError, "measurement"):
            await session.register_speaker_job("align")
        self.assertEqual(session.active_manual_job_ids, {"manual"})


if __name__ == "__main__":
    unittest.main()
