#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""SMB discovery rescan behavior: fresh hosts per cycle, parallel probes.

* Hosts appearing after service start must be found without a restart:
  the neighbor table is re-read on every discovery cycle, not frozen at
  construction (explicit host lists stay frozen).
* One slow/unreachable host must not serialize the whole share list
  behind it: candidates are probed in parallel with bounded timeouts.
* STALE/FAILED neighbor entries are still probed (STALE is the normal
  steady state of a live host); only non-IPv4 lines are skipped.
* The 30-s result cache is unchanged.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library import sources
from library.sources import MusicLibraryManager


def _manager():
    tmp = tempfile.TemporaryDirectory()
    return tmp, MusicLibraryManager(Path(tmp.name) / "Music")


def main_test():
    # Late-appearing host found without a restart ----------------------
    tmp, manager = _manager()
    try:
        host_table = {"hosts": []}
        with mock.patch.object(
            sources, "default_discovery_hosts", side_effect=lambda: list(host_table["hosts"])
        ), mock.patch.object(
            sources, "discover_smb_shares", return_value=[]
        ):
            entries = manager.list_libraries()
            assert [e["id"] for e in entries] == ["local", "manual"], entries
            host_table["hosts"] = ["newhost"]
            manager._discovered_at = 0.0
            with mock.patch.object(
                sources,
                "discover_smb_shares",
                return_value=[{
                    "id": "smb:newhost:Music", "type": "smb", "label": "x",
                    "server": "newhost", "share": "Music",
                }],
            ) as discover:
                entries = manager.list_libraries()
                assert "smb:newhost:Music" in [e["id"] for e in entries], entries
                discover.assert_called_once_with(["newhost"])
            assert manager.discovery_hosts == ["newhost"], manager.discovery_hosts
        print("late-appearing host found without restart: ok")

        # Explicit host lists stay frozen --------------------------------
        with mock.patch.object(sources, "default_discovery_hosts", return_value=["other"]), \
             mock.patch.object(sources, "discover_smb_shares", return_value=[]) as discover:
            fixed = MusicLibraryManager(Path(tmp.name) / "Fixed", discovery_hosts=["frozen"])
            fixed._discovered_at = 0.0
            fixed.list_libraries()
            discover.assert_called_once_with(["frozen"])
        print("explicit host list stays frozen: ok")
    finally:
        tmp.cleanup()

    # Slow host does not delay reachable shares -------------------------
    marks = {}

    class Result:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = ""

    def fake_run(command, **_kwargs):
        if "-L" in command:
            target = command[command.index("-L") + 1]
            if "slowhost" in target:
                time.sleep(3.0)
                return Result(stdout="Disk|SlowShare|comment\n")
            if "fashost" in target:
                marks["fast-list"] = time.monotonic()
                return Result(stdout="Disk|FastShare|comment\n")
        joined = " ".join(command)
        if "FastShare" in joined:
            marks["fast-ls"] = time.monotonic()
            return Result(stdout="  .                                   D        0  Jan 01 00:00 .\n")
        if "SlowShare" in joined:
            return Result(stdout="  .                                   D        0  Jan 01 00:00 .\n")
        raise AssertionError(f"unexpected command: {command}")

    with mock.patch.object(sources.subprocess, "run", side_effect=fake_run):
        started = time.monotonic()
        found = sources.discover_smb_shares(["slowhost", "fashost"])
        elapsed = time.monotonic() - started
    ids = sorted(e["id"] for e in found)
    assert ids == ["smb:fashost:FastShare", "smb:slowhost:SlowShare"], ids
    # The fast host is fully probed while the slow one is still blocked in
    # its 3 s listing: reachable shares never wait behind it.
    assert marks["fast-list"] - started < 1.5, marks
    assert marks["fast-ls"] - started < 1.5, marks
    assert elapsed < 5.0, f"cycle took too long: {elapsed:.2f}s"
    print(f"slow host does not delay reachable shares: ok ({elapsed:.2f}s)")

    # STALE/FAILED neighbors are still probed ------------------------------
    neigh = (
        "192.168.178.1 dev wlan0 lladdr 00:11:22:33:44:55 REACHABLE\n"
        "192.168.178.50 dev wlan0 lladdr 00:11:22:33:44:56 STALE\n"
        "192.168.178.60 dev wlan0  FAILED\n"
        "fe80::1 dev wlan0 lladdr 00:11:22:33:44:57 router REACHABLE\n"
        "garbage line without address\n"
        "\n"
    )

    def fake_ip(command, **_kwargs):
        assert command[:3] == ["ip", "neigh", "show"], command
        return Result(stdout=neigh)

    with mock.patch.object(sources.subprocess, "run", side_effect=fake_ip), \
         mock.patch.dict(os.environ, {"MUSIC_LIBRARY_SMB_HOSTS": ""}):
        hosts = sources.default_discovery_hosts()
    assert hosts == ["192.168.178.1", "192.168.178.50", "192.168.178.60"], hosts
    print("stale/failed neighbors still probed, non-IPv4 skipped: ok")

    print("SMB discovery rescan: ok")


if __name__ == "__main__":
    main_test()
