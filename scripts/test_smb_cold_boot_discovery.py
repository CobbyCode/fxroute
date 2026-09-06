#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Cold-boot SMB discovery: empty neighbor table still finds the LAN server.

Freshly booted FXRoute has no learned ARP/neighbor entries, so discovery
must not depend on `ip neigh show` alone. The automatic path actively sweeps
the local IPv4 networks for open SMB ports (TCP 445) and probes those hosts
exactly like before. The explicit MUSIC_LIBRARY_SMB_HOSTS path stays a
fast deterministic shortcut without any subprocess or socket I/O.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library import sources


SMB_SERVER = "192.168.178.100"

BOOT_NEIGH_EMPTY = ""

BOOT_NEIGH_WITH_DEAD = (
    "192.168.178.50 dev eth0 lladdr 00:11:22:33:44:56 STALE\n"
    f"{SMB_SERVER} dev eth0 lladdr 00:e0:4c:4b:1a:0c REACHABLE\n"
)

BOOT_ADDR_SHOW = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
    "2: eth0    inet 192.168.178.126/24 brd 192.168.178.255 scope global dynamic eth0\\"
    "       valid_lft forever preferred_lft forever\n"
)


class _Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


BOOT_NEIGH = {"out": BOOT_NEIGH_EMPTY}


def _fake_run(command, **_kwargs):
    if command[:3] == ["ip", "neigh", "show"]:
        return _Result(stdout=BOOT_NEIGH["out"])
    if command[:5] == ["ip", "-o", "-4", "addr", "show"]:
        return _Result(stdout=BOOT_ADDR_SHOW)
    if command[0] == "smbclient" and "-L" in command:
        target = command[command.index("-L") + 1]
        assert target == f"//{SMB_SERVER}", target
        return _Result(stdout="Disk|Music-Demo|FXRoute Demo Library\n")
    if command[0] == "smbclient" and f"//{SMB_SERVER}/Music-Demo" in command:
        return _Result(stdout="  .                                   D        0  Jan 01 00:00 .\n")
    raise AssertionError(f"unexpected command (cold-boot must stay minimal): {command}")


def _fake_create_connection(address, timeout=None, **_kwargs):
    host, _port = address
    if host == SMB_SERVER:
        class _Dummy:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Dummy()
    raise ConnectionRefusedError(f"cold-boot fixture: no SMB on {host}")


def main_test():
    # Empty neighbor table after boot -> server still found via active sweep.
    with mock.patch.object(sources.subprocess, "run", side_effect=_fake_run), \
         mock.patch.object(sources.socket, "create_connection", side_effect=_fake_create_connection), \
         mock.patch.dict(os.environ, {"MUSIC_LIBRARY_SMB_HOSTS": ""}):
        hosts = sources.default_discovery_hosts()
    assert SMB_SERVER in hosts, f"active sweep missed {SMB_SERVER}: {hosts}"
    assert "192.168.178.126" not in hosts, f"own address must be excluded: {hosts}"
    # Only the SMB host needs the slower smbclient probes, not the whole /24.
    with mock.patch.object(sources.subprocess, "run", side_effect=_fake_run):
        shares = sources.discover_smb_shares(hosts)
    ids = [entry["id"] for entry in shares]
    assert f"smb:{SMB_SERVER}:Music-Demo" in ids, ids
    print(f"empty neighbor table still finds {SMB_SERVER}/Music-Demo: ok (hosts={hosts})")

    # Swept-closed neighbors are skipped for smbclient: .50 is a known LAN
    # member but the sweep proved both SMB ports closed there, so only the
    # real server is probed (same shares, none of the ~3.4 s dead-host cost).
    BOOT_NEIGH["out"] = BOOT_NEIGH_WITH_DEAD
    try:
        with mock.patch.object(sources.subprocess, "run", side_effect=_fake_run), \
             mock.patch.object(sources.socket, "create_connection", side_effect=_fake_create_connection), \
             mock.patch.dict(os.environ, {"MUSIC_LIBRARY_SMB_HOSTS": ""}):
            hosts = sources.default_discovery_hosts()
    finally:
        BOOT_NEIGH["out"] = BOOT_NEIGH_EMPTY
    assert hosts == [SMB_SERVER], f"swept-closed neighbor must be skipped: {hosts}"
    with mock.patch.object(sources.subprocess, "run", side_effect=_fake_run):
        shares = sources.discover_smb_shares(hosts)
    assert [e["id"] for e in shares] == [f"smb:{SMB_SERVER}:Music-Demo"], shares
    print("swept-closed neighbors skipped without losing shares: ok")

    # Explicit host list stays the fast deterministic path (no I/O at all).
    with mock.patch.object(sources.subprocess, "run") as run, \
         mock.patch.object(sources.socket, "create_connection") as conn, \
         mock.patch.dict(os.environ, {"MUSIC_LIBRARY_SMB_HOSTS": "OpenClaw@192.168.178.100"}):
        hosts = sources.default_discovery_hosts()
    assert hosts == ["OpenClaw@192.168.178.100"], hosts
    run.assert_not_called()
    conn.assert_not_called()
    print("configured MUSIC_LIBRARY_SMB_HOSTS still skips discovery I/O: ok")

    print("SMB cold-boot discovery: ok")


if __name__ == "__main__":
    main_test()
