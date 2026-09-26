#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""AutoSub discovery uses the same live jobs as poll and cancel."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.autosub import deps, jobs


class AutoSubReconnectTests(unittest.TestCase):
    def test_current_job_returns_latest_live_or_retained_terminal_job(self):
        keys = ("auto-sub-reconnect-old", "auto-sub-reconnect-new")
        try:
            self.assertIsNone(asyncio.run(jobs.get_current_auto_sub_optimize_job())["job"])
            deps._AUTO_SUB_JOBS[keys[0]] = {"id": keys[0], "status": "completed", "result": {"winner": 1}}
            deps._AUTO_SUB_JOBS[keys[1]] = {"id": keys[1], "status": "running", "progress": {"current": 2}}
            self.assertIs(asyncio.run(jobs.get_current_auto_sub_optimize_job())["job"], deps._AUTO_SUB_JOBS[keys[1]])
            deps._AUTO_SUB_JOBS[keys[1]]["status"] = "cancelled"
            self.assertEqual(asyncio.run(jobs.get_current_auto_sub_optimize_job())["job"]["status"], "cancelled")
        finally:
            for key in keys:
                deps._AUTO_SUB_JOBS.pop(key, None)


if __name__ == "__main__":
    unittest.main()
