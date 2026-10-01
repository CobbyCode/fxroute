#!/usr/bin/env python3
"""Check that failed live verification cannot leak test processes."""
import signal
import subprocess
import sys
import unittest

import verify_native_worker_swaps as verifier


class CleanupTests(unittest.TestCase):
    def test_shutdown_timeout_reaps_hung_child_and_later_children(self):
        children = []
        try:
            stubborn = subprocess.Popen([sys.executable, "-c",
                "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                "print('ready',flush=True); time.sleep(30)"], stdout=subprocess.PIPE, text=True)
            children.append(stubborn)
            self.assertEqual(stubborn.stdout.readline(), "ready\n")
            later = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            children.append(later)
            failures = verifier.stop_processes(children, timeout=.05)
            self.assertTrue(failures)
            self.assertEqual(stubborn.returncode, -signal.SIGKILL)
            self.assertIsNotNone(later.returncode)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
                if child.stdout:
                    child.stdout.close()


if __name__ == "__main__":
    unittest.main()
