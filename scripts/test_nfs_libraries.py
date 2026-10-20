#!/usr/bin/env python3
"""Focused network-library contracts; no real mounts or LAN probes."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from library.sources import MusicLibraryManager, default_discovery_hosts


class NfsLibraryTests(unittest.TestCase):
    def test_manual_nfs_and_smb_share_one_list(self):
        manager = MusicLibraryManager(Path("/music"), discovery_hosts=[])
        smb = manager.add_manual_url("smb://nas/Music")
        nfs = manager.add_manual_url("nas:/exports/My Music/")
        self.assertEqual(nfs, {
            "id": "nfs:nas:/exports/My Music", "type": "nfs",
            "label": "NFS — nas:/exports/My Music", "server": "nas",
            "share": "/exports/My Music",
        })
        self.assertEqual([e["id"] for e in manager.status_cached()["libraries"]],
                         ["local", smb["id"], nfs["id"], "manual"])

    def test_manual_nfs_accepts_ipv6_and_root_export(self):
        manager = MusicLibraryManager(Path("/music"), discovery_hosts=[])
        self.assertEqual(manager.add_manual_url("[2001:db8::1]:/")["server"], "[2001:db8::1]")
        self.assertEqual(manager.add_manual_url("nas://music///")["share"], "/music")

    def test_unsafe_nfs_input_cannot_reach_helper(self):
        manager = MusicLibraryManager(Path("/music"), discovery_hosts=[])
        for url in ("-o:/music", "--remove-all:/music", "nas:relative", "nas:/a/../b",
                    "nas:/a/./b", "nas:/music\nextra", "nas:/a,b", "nas:/a\\040b",
                    "nas:/music#comment", "nfs://user:password@nas/music"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                manager.add_manual_url(url)

    def test_nfs_host_sweep_uses_nfs_ports_and_independent_override(self):
        with patch.dict("os.environ", {"MUSIC_LIBRARY_NFS_HOSTS": "nas,backup"}), patch(
            "library.sources._neighbor_hosts", side_effect=AssertionError("override must skip sweep")
        ):
            self.assertEqual(default_discovery_hosts(protocol="nfs"), ["nas", "backup"])
        with patch.dict("os.environ", {}, clear=True), patch(
            "library.sources._neighbor_hosts", return_value=[]
        ), patch("library.sources._local_ipv4_networks", return_value=(["network"], set())), patch(
            "library.sources._sweep_open_hosts", return_value=["192.0.2.7"]
        ) as sweep:
            self.assertEqual(default_discovery_hosts(protocol="nfs"), ["192.0.2.7"])
        self.assertEqual(sweep.call_args.kwargs["ports"], (2049, 111))

    def test_discovery_merges_smb_and_nfs(self):
        manager = MusicLibraryManager(Path("/music"), discovery_hosts=["nas"])

        def run(command, **kwargs):
            if command[0] == "smbclient":
                output = "Disk|Music|Audio\n" if "-L" in command else "album\n"
            elif command[0] == "showmount":
                output = "/exports/music *\n/exports/music *\n"
            elif command[:4] == ["sudo", "-n", "/usr/local/sbin/fxroute-cifs-mount", "--discover-nfs"]:
                return subprocess.CompletedProcess(command, 1, "", "v4 unavailable")
            else:
                self.fail(f"Unexpected discovery command: {command}")
            return subprocess.CompletedProcess(command, 0, output, "")

        with patch("subprocess.run", side_effect=run):
            entries = manager.list_libraries()
        self.assertEqual([e["type"] for e in entries], ["local", "smb", "nfs", "action"])

    def test_nfs4_namespace_fallback_and_failed_hosts(self):
        from library.nfs import discover_nfs_shares

        def run(command, **kwargs):
            if command[0] == "showmount":
                raise subprocess.TimeoutExpired(command, 4)
            self.assertEqual(command[:4], ["sudo", "-n", "/usr/local/sbin/fxroute-cifs-mount", "--discover-nfs"])
            if command[-1] == "offline":
                return subprocess.CompletedProcess(command, 1, "", "unreachable")
            return subprocess.CompletedProcess(command, 0, "/\n/Music\n/../escape\n/Music\n", "")

        with patch("subprocess.run", side_effect=run):
            entries = discover_nfs_shares(["NAS@nas", "offline"])
        self.assertEqual([e["id"] for e in entries], ["nfs:nas:/", "nfs:nas:/Music"])
        self.assertEqual(entries[1]["label"], "NFS — NAS:/Music")

    def test_v4_namespace_is_discovered_even_when_mountd_lists_physical_paths(self):
        from library.nfs import discover_nfs_shares

        def run(command, **kwargs):
            output = "/srv/physical/music *\n" if command[0] == "showmount" else "/\n/Music\n"
            return subprocess.CompletedProcess(command, 0, output, "")

        with patch("subprocess.run", side_effect=run):
            entries = discover_nfs_shares(["nas"])
        self.assertIn("nfs:nas:/Music", [e["id"] for e in entries])

    def test_nfs_activation_requires_real_nfs_mount_and_uses_helper(self):
        from library import nfs

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = MusicLibraryManager(root / "Music", mount_root=root / "mounts", discovery_hosts=[])
            entry = manager.add_manual_url("nas:/exports/music")
            digest = hashlib.sha256(b"/exports/music").hexdigest()
            mounted = root / "mounts-nfs" / "nas" / digest
            mounts = {str(mounted): ("autofs", "systemd-1")}

            def run(command, **kwargs):
                self.assertEqual(command, ["sudo", "-n", "/usr/local/sbin/fxroute-cifs-mount", "--nfs", "nas", "/exports/music"])
                mounts[str(mounted)] = ("nfs4", "nas:/exports/music")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(nfs, "read_mounts", return_value=mounts), patch("subprocess.run", side_effect=run):
                self.assertEqual(manager.activate(entry["id"]), mounted)
            self.assertEqual(manager.capture_context().library_type, "nfs")

    def test_nfs_mount_failure_does_not_publish_or_use_gvfs(self):
        with tempfile.TemporaryDirectory() as td:
            manager = MusicLibraryManager(Path(td), discovery_hosts=[])
            entry = manager.add_manual_url("nas:/music")
            with patch("library.nfs.read_mounts", return_value={}), patch(
                "subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "access denied")
            ) as run:
                with self.assertRaisesRegex(FileNotFoundError, "access denied"):
                    manager.activate(entry["id"])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(manager.active_id, "local")

    def test_nfs_mount_paths_cannot_overlap_any_legacy_smb_mount(self):
        from library import nfs
        path = nfs.mount_path(Path("/mounts/1000"), "nas", "/music")
        self.assertFalse(path.is_relative_to(Path("/mounts/1000")))
        self.assertEqual(path.parent, Path("/mounts/1000-nfs/nas"))

    def test_saved_shares_and_selection_survive_restart_without_discovery(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "libraries.json"
            manager = MusicLibraryManager(Path(td), discovery_hosts=[], state_path=path)
            manager.add_manual_url("smb://nas/Music")
            entry = manager.add_manual_url("nas:/music")
            with patch.object(manager, "_mounted_nfs_path", return_value=Path("/mounted")):
                manager.activate(entry["id"])
            restarted = MusicLibraryManager(Path(td), discovery_hosts=[], state_path=path)
            self.assertEqual(restarted.saved_active_id, entry["id"])
            self.assertEqual([e["type"] for e in restarted.status_cached()["libraries"]],
                             ["local", "smb", "nfs", "action"])
            with patch.object(restarted, "_mounted_nfs_path", return_value=Path("/mounted")):
                restarted.activate(restarted.saved_active_id)
            self.assertEqual(restarted.active_type, "nfs")

    def test_discovered_selection_is_saved_before_discovery_disappears(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "libraries.json"
            manager = MusicLibraryManager(Path(td), discovery_hosts=[], state_path=path)
            entry = manager.add_manual_url("nas:/music")
            manager._discovered = [entry]
            manager._manual.clear()
            with patch.object(manager, "_mounted_nfs_path", return_value=Path("/mounted")):
                manager.activate(entry["id"])
            restarted = MusicLibraryManager(Path(td), discovery_hosts=[], state_path=path)
            self.assertIn(entry["id"], [e["id"] for e in restarted.status_cached()["libraries"]])

    def test_removal_is_persistent_and_does_not_touch_other_protocol(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "libraries.json"
            manager = MusicLibraryManager(Path(td), discovery_hosts=[], state_path=path)
            smb = manager.add_manual_url("smb://nas/Music")
            nfs = manager.add_manual_url("nas:/music")
            manager._discovered = [nfs]
            with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                manager.remove_library(nfs["id"])
            self.assertEqual(run.call_args.args[0][-4:], ["--remove", "nfs", "nas", "/music"])
            restarted = MusicLibraryManager(Path(td), discovery_hosts=[], state_path=path)
            restarted._discovered = [nfs]
            self.assertEqual([e["id"] for e in restarted.status_cached()["libraries"]],
                             ["local", smb["id"], "manual"])

    def test_failed_removal_keeps_saved_entry(self):
        manager = MusicLibraryManager(Path("/music"), discovery_hosts=[])
        entry = manager.add_manual_url("nas:/music")
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "busy")):
            with self.assertRaisesRegex(ValueError, "busy"):
                manager.remove_library(entry["id"])
        self.assertIn(entry["id"], [e["id"] for e in manager.status_cached()["libraries"]])

    def test_corrupt_state_does_not_prevent_local_library_start(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "libraries.json"
            for content in ("{bad", json.dumps({"libraries": [None, {"type": "nfs", "server": "--remove-all", "share": "/"}]}),
                            json.dumps({"libraries": [{"type": "nfs", "server": None, "share": "/"}]})):
                path.write_text(content)
                manager = MusicLibraryManager(Path(td), discovery_hosts=[], state_path=path)
                self.assertEqual(manager.saved_active_id, "local")
                self.assertEqual(len(manager.status_cached()["libraries"]), 2)


if __name__ == "__main__":
    unittest.main()
