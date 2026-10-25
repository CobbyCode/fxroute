#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Run one test in an owned process group and reap it within a time limit."""

import os
import signal
import subprocess
import sys


def signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def run(seconds, command):
    process = subprocess.Popen(command, start_new_session=True)

    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        try:
            status = process.wait(timeout=seconds)
            return status if status >= 0 else 128 - status
        except subprocess.TimeoutExpired:
            signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return 124
    finally:
        # The direct child may exit on TERM while descendants keep stdout open.
        signal_group(process, signal.SIGKILL)
        process.wait()


if __name__ == '__main__':
    raise SystemExit(run(int(sys.argv[1]), sys.argv[2:]))
