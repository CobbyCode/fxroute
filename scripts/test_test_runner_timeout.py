#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""The shared runner bounds hung files and still runs subsequent tests."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RunnerTimeoutTests(unittest.TestCase):
    def test_hung_file_fails_and_following_file_runs_in_both_output_modes(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                scripts = root / "scripts"
                scripts.mkdir()
                shutil.copy2(ROOT / "scripts/run_tests.sh", scripts / "run_tests.sh")
                helper = ROOT / "scripts/run_test_with_timeout.py"
                shutil.copy2(helper, scripts / helper.name)
                group_file = root / "test-group"
                child_file = root / "test-child"
                (scripts / "test_a_hangs.py").write_text(
                    "import os, pathlib, subprocess, sys, time\n"
                    f"pathlib.Path({str(group_file)!r}).write_text(str(os.getpgrp()))\n"
                    "subprocess.Popen([sys.executable, '-c', "
                    f"\"import os, pathlib, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    f"pathlib.Path({str(child_file)!r}).write_text(str(os.getpid())); time.sleep(60)\"])\n"
                    "time.sleep(60)\n")
                (scripts / "test_b_follows.py").write_text("print('next file ran')\n")
                (scripts / "test_c_skips.py").write_text("raise SystemExit(77)\n")
                # No native files are staged; suppress host-specific native prerequisites.
                binary = root / "bin"
                binary.mkdir()
                probe = binary / "pkg-config"
                probe.write_text("#!/bin/sh\nexit 1\n")
                probe.chmod(0o755)
                env = dict(os.environ, FXROUTE_TEST_TIMEOUT_SECONDS="1",
                           PATH=str(binary) + os.pathsep + os.environ["PATH"])
                args = ["bash", str(scripts / "run_tests.sh")]
                if verbose:
                    args.append("--verbose")
                with subprocess.Popen(args, env=env, text=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, start_new_session=True) as process:
                    try:
                        stdout, stderr = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        if group_file.exists():
                            try:
                                os.killpg(int(group_file.read_text()), signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate(timeout=2)
                        self.fail("Shared runner did not bound the hung test file")
                    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
                stat = Path(f"/proc/{int(child_file.read_text())}/stat") if child_file.exists() else None
                try:
                    child_running = bool(stat and stat.read_text().rsplit(')', 1)[1].split()[0] != 'Z')
                except FileNotFoundError:
                    child_running = False
                if group_file.exists():
                    try:
                        os.killpg(int(group_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("FAIL  scripts/test_a_hangs.py (timeout after 1s)", result.stdout)
                self.assertIn("PASS  scripts/test_b_follows.py", result.stdout)
                self.assertIn("SKIP  scripts/test_c_skips.py", result.stdout)
                self.assertIn("  failed: 1", result.stdout)
                self.assertTrue(child_file.exists(), "The TERM-resistant descendant must have started")
                self.assertFalse(child_running, "Timed-out descendant is still running")


if __name__ == "__main__":
    unittest.main()
