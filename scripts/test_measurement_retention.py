#!/usr/bin/env python3
"""Retention: IR debug segments and job records are bounded, and nothing
outside FXRoute-owned directories is ever deleted."""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement import (
    IR_DEBUG_SEGMENT_RETENTION_SEGMENTS,
    JOB_RECORD_RETENTION_DAYS,
    MeasurementStore,
)


def _old_timestamp(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")


class MeasurementRetentionTests(unittest.TestCase):
    def _store(self, tempdir):
        return MeasurementStore(home=Path(tempdir))

    def test_ir_debug_segments_pruned_to_newest(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            output_dir = store.diagnostics_dir / "impulse-ir"
            output_dir.mkdir(parents=True, exist_ok=True)
            total = IR_DEBUG_SEGMENT_RETENTION_SEGMENTS + 5
            for index in range(total):
                (output_dir / f"seg-{index:02d}.json").write_text("{}")
                (output_dir / f"seg-{index:02d}.csv").write_text("x")
                # Newest segments get the newest mtimes.
                stamp = time.time() - (total - index)
                os.utime(output_dir / f"seg-{index:02d}.json", (stamp, stamp))
                os.utime(output_dir / f"seg-{index:02d}.csv", (stamp, stamp))

            store._prune_ir_debug_segments(output_dir)

            remaining_json = sorted(path.name for path in output_dir.glob("*.json"))
            remaining_csv = sorted(path.name for path in output_dir.glob("*.csv"))
            self.assertEqual(len(remaining_json), IR_DEBUG_SEGMENT_RETENTION_SEGMENTS)
            self.assertEqual(len(remaining_csv), IR_DEBUG_SEGMENT_RETENTION_SEGMENTS)
            # The newest segments survive, oldest are gone.
            self.assertIn(f"seg-{total - 1:02d}.json", remaining_json)
            self.assertNotIn("seg-00.json", remaining_json)
            self.assertNotIn("seg-00.csv", remaining_csv)

    def test_ir_debug_segment_save_triggers_pruning(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            output_dir = store.diagnostics_dir / "impulse-ir"
            output_dir.mkdir(parents=True, exist_ok=True)
            for index in range(IR_DEBUG_SEGMENT_RETENTION_SEGMENTS + 2):
                (output_dir / f"old-{index:02d}.json").write_text("{}")
                (output_dir / f"old-{index:02d}.csv").write_text("x")

            segment = {
                "schema": "fxroute.measurement.ir-debug-segment.v1",
                "channel": "left",
                "sample_rate": 48_000,
                "markers": {},
                "candidate_markers": [],
                "segment": [{
                    "sample": 0,
                    "offset_from_global_peak_samples": 0,
                    "offset_from_selected_direct_samples": 0,
                    "value_normalized": 0.0,
                    "abs_normalized": 0.0,
                }],
            }
            saved = store._save_impulse_response_debug_segment("sweep-new", segment)
            self.assertTrue(saved.get("json_path"))

            self.assertEqual(len(list(output_dir.glob("*.json"))), IR_DEBUG_SEGMENT_RETENTION_SEGMENTS)
            self.assertEqual(len(list(output_dir.glob("*.csv"))), IR_DEBUG_SEGMENT_RETENTION_SEGMENTS)

    def test_old_terminal_job_records_are_retained_out(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            old_id = "measurement-job-old"
            fresh_id = "measurement-job-fresh"
            old_record = {
                "id": old_id,
                "status": "completed",
                "created_at": _old_timestamp(JOB_RECORD_RETENTION_DAYS + 1),
                "updated_at": _old_timestamp(JOB_RECORD_RETENTION_DAYS + 1),
                "message": "old",
                "result": {"huge": "payload"},
                "error": None,
            }
            fresh_record = {
                "id": fresh_id,
                "status": "completed",
                "created_at": store._utc_now(),
                "updated_at": store._utc_now(),
                "message": "fresh",
                "result": None,
                "error": None,
            }
            store._jobs[old_id] = dict(old_record)
            store._jobs[fresh_id] = dict(fresh_record)
            store._job_tasks[old_id] = None
            store._cancelled_jobs.add(old_id)
            (store.job_records_dir / f"{old_id}.json").write_text(json.dumps(old_record))
            (store.job_records_dir / f"{fresh_id}.json").write_text(json.dumps(fresh_record))

            store._retain_job_history()

            self.assertNotIn(old_id, store._jobs)
            self.assertNotIn(old_id, store._job_tasks)
            self.assertNotIn(old_id, store._cancelled_jobs)
            self.assertFalse((store.job_records_dir / f"{old_id}.json").exists())
            self.assertIn(fresh_id, store._jobs)
            self.assertTrue((store.job_records_dir / f"{fresh_id}.json").exists())

    def test_disk_only_old_terminal_records_are_retained_out(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            old_id = "measurement-job-disk-only"
            (store.job_records_dir / f"{old_id}.json").write_text(json.dumps({
                "id": old_id,
                "status": "failed",
                "created_at": _old_timestamp(JOB_RECORD_RETENTION_DAYS + 2),
                "updated_at": _old_timestamp(JOB_RECORD_RETENTION_DAYS + 2),
                "message": "old",
                "result": None,
                "error": {"detail": "boom"},
            }))

            store._retain_job_history()

            self.assertFalse((store.job_records_dir / f"{old_id}.json").exists())

    def test_active_and_nonterminal_records_are_never_touched(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            running_id = "measurement-job-running-old"
            queued_id = "measurement-job-queued-old"
            for job_id, status in ((running_id, "running"), (queued_id, "queued")):
                store._jobs[job_id] = {
                    "id": job_id,
                    "status": status,
                    "created_at": _old_timestamp(JOB_RECORD_RETENTION_DAYS + 1),
                    "updated_at": _old_timestamp(JOB_RECORD_RETENTION_DAYS + 1),
                    "message": "active",
                    "result": None,
                    "error": None,
                }
                (store.job_records_dir / f"{job_id}.json").write_text(json.dumps(store._jobs[job_id]))

            store._retain_job_history()

            self.assertIn(running_id, store._jobs)
            self.assertIn(queued_id, store._jobs)
            self.assertTrue((store.job_records_dir / f"{running_id}.json").exists())
            self.assertTrue((store.job_records_dir / f"{queued_id}.json").exists())

    def test_retention_never_touches_user_measurements(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            user_path = store.measurements_dir / "saved-measurement.json"
            user_path.parent.mkdir(parents=True, exist_ok=True)
            user_path.write_text('{"id": "saved-measurement"}')
            store._jobs["measurement-job-old"] = {
                "id": "measurement-job-old",
                "status": "cancelled",
                "created_at": _old_timestamp(JOB_RECORD_RETENTION_DAYS + 3),
                "updated_at": _old_timestamp(JOB_RECORD_RETENTION_DAYS + 3),
                "message": "old",
                "result": None,
                "error": None,
            }

            store._retain_job_history()

            self.assertTrue(user_path.exists())
            self.assertTrue((store.measurements_dir / "saved-measurement.json").exists())

    def test_job_record_symlink_is_never_followed(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            outside = Path(tempdir) / "outside-secret.json"
            outside.write_text('{"secret": true}')
            link = store.job_records_dir / "measurement-job-evil.json"
            link.symlink_to(outside)

            store._retain_job_history()

            self.assertEqual(outside.read_text(), '{"secret": true}')
            self.assertTrue(link.is_symlink())

    def test_ir_debug_symlinks_are_never_followed(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            output_dir = store.diagnostics_dir / "impulse-ir"
            output_dir.mkdir(parents=True, exist_ok=True)
            total = IR_DEBUG_SEGMENT_RETENTION_SEGMENTS + 1
            for index in range(total):
                (output_dir / f"seg-{index:02d}.json").write_text("{}")
                (output_dir / f"seg-{index:02d}.csv").write_text("x")
                stamp = time.time() - (total - index)
                os.utime(output_dir / f"seg-{index:02d}.json", (stamp, stamp))
                os.utime(output_dir / f"seg-{index:02d}.csv", (stamp, stamp))
            outside = Path(tempdir) / "outside-ir.json"
            outside.write_text('{"outside": true}')
            outside_csv = Path(tempdir) / "outside-ir.csv"
            outside_csv.write_text("outside")
            (output_dir / "evil.json").symlink_to(outside)
            (output_dir / "evil.csv").symlink_to(outside_csv)

            store._prune_ir_debug_segments(output_dir)

            self.assertEqual(outside.read_text(), '{"outside": true}')
            self.assertEqual(outside_csv.read_text(), "outside")
            self.assertTrue((output_dir / "evil.json").is_symlink())
            self.assertTrue((output_dir / "evil.csv").is_symlink())

    def test_naive_and_malformed_timestamps_never_break_retention(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            naive_id = "measurement-job-naive"
            malformed_id = "measurement-job-malformed"
            record = {
                "id": naive_id,
                "status": "completed",
                "created_at": "2020-01-01T00:00:00",
                "updated_at": "2020-01-01T00:00:00",
                "message": "naive",
                "result": None,
                "error": None,
            }
            store._jobs[naive_id] = dict(record)
            (store.job_records_dir / f"{naive_id}.json").write_text(json.dumps(record))
            store._jobs[malformed_id] = {
                "id": malformed_id,
                "status": "completed",
                "created_at": "not-a-date",
                "updated_at": "not-a-date",
                "message": "malformed",
                "result": None,
                "error": None,
            }

            # Must never raise (would break job finalization / measurement start).
            store._retain_job_history()

            # Naive timestamps are normalized to UTC and retained out when old;
            # malformed timestamps are skipped conservatively and kept.
            self.assertNotIn(naive_id, store._jobs)
            self.assertFalse((store.job_records_dir / f"{naive_id}.json").exists())
            self.assertIn(malformed_id, store._jobs)

    def test_ir_pair_with_symlinked_csv_is_kept_untouched(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            output_dir = store.diagnostics_dir / "impulse-ir"
            output_dir.mkdir(parents=True, exist_ok=True)
            total = IR_DEBUG_SEGMENT_RETENTION_SEGMENTS + 1
            for index in range(total):
                (output_dir / f"seg-{index:02d}.json").write_text("{}")
                (output_dir / f"seg-{index:02d}.csv").write_text("x")
                stamp = time.time() - (total - index)
                os.utime(output_dir / f"seg-{index:02d}.json", (stamp, stamp))
                os.utime(output_dir / f"seg-{index:02d}.csv", (stamp, stamp))
            outside_csv = Path(tempdir) / "outside-ir.csv"
            outside_csv.write_text("outside")
            # The oldest pair (beyond the retention limit) has a symlinked CSV.
            (output_dir / "seg-00.csv").unlink()
            (output_dir / "seg-00.csv").symlink_to(outside_csv)

            store._prune_ir_debug_segments(output_dir)

            self.assertTrue((output_dir / "seg-00.json").exists())
            self.assertTrue((output_dir / "seg-00.csv").is_symlink())
            self.assertEqual(outside_csv.read_text(), "outside")

    def test_ir_pair_with_symlinked_json_keeps_csv_untouched(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            output_dir = store.diagnostics_dir / "impulse-ir"
            output_dir.mkdir(parents=True, exist_ok=True)
            for index in range(IR_DEBUG_SEGMENT_RETENTION_SEGMENTS):
                (output_dir / f"seg-{index:02d}.json").write_text("{}")
                (output_dir / f"seg-{index:02d}.csv").write_text("x")
                stamp = time.time() - (IR_DEBUG_SEGMENT_RETENTION_SEGMENTS - index)
                os.utime(output_dir / f"seg-{index:02d}.json", (stamp, stamp))
                os.utime(output_dir / f"seg-{index:02d}.csv", (stamp, stamp))
            outside = Path(tempdir) / "outside-ir.json"
            outside.write_text('{"outside": true}')
            (output_dir / "seg-x.csv").write_text("x")
            (output_dir / "seg-x.json").symlink_to(outside)

            store._prune_ir_debug_segments(output_dir)

            self.assertTrue((output_dir / "seg-x.csv").exists())
            self.assertTrue((output_dir / "seg-x.json").is_symlink())
            self.assertEqual(outside.read_text(), '{"outside": true}')

    def test_failed_csv_unlink_is_retried_by_later_prune(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ", {"XDG_CONFIG_HOME": tempdir, "XDG_STATE_HOME": tempdir}
        ):
            store = self._store(tempdir)
            output_dir = store.diagnostics_dir / "impulse-ir"
            output_dir.mkdir(parents=True, exist_ok=True)
            total = IR_DEBUG_SEGMENT_RETENTION_SEGMENTS + 2
            for index in range(total):
                (output_dir / f"seg-{index:02d}.json").write_text("{}")
                (output_dir / f"seg-{index:02d}.csv").write_text("x")
                stamp = time.time() - (total - index)
                os.utime(output_dir / f"seg-{index:02d}.json", (stamp, stamp))
                os.utime(output_dir / f"seg-{index:02d}.csv", (stamp, stamp))

            original_unlink = Path.unlink
            failed = {"done": False}

            def _flaky_unlink(path_self, *args, **kwargs):
                if str(path_self).endswith("seg-00.csv") and not failed["done"]:
                    failed["done"] = True
                    raise PermissionError("denied")
                return original_unlink(path_self, *args, **kwargs)

            with patch.object(Path, "unlink", _flaky_unlink):
                store._prune_ir_debug_segments(output_dir)
            # First run: seg-00 CSV removal failed, its JSON stays intact.
            self.assertTrue((output_dir / "seg-00.json").exists())
            self.assertTrue((output_dir / "seg-00.csv").exists())

            # Second run: the pair can now be removed completely.
            store._prune_ir_debug_segments(output_dir)

            self.assertEqual(len(list(output_dir.glob("*.json"))), IR_DEBUG_SEGMENT_RETENTION_SEGMENTS)
            self.assertEqual(len(list(output_dir.glob("*.csv"))), IR_DEBUG_SEGMENT_RETENTION_SEGMENTS)
            self.assertFalse((output_dir / "seg-00.json").exists())
            self.assertFalse((output_dir / "seg-00.csv").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
