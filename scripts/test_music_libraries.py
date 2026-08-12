#!/usr/bin/env python3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from music_libraries import MusicLibraryManager, default_discovery_hosts, discover_smb_shares


class MusicLibraryDiscoveryTests(unittest.TestCase):
    def test_default_discovery_includes_live_lan_neighbors(self):
        result = subprocess.CompletedProcess([], 0, "192.168.178.100 dev eth0 lladdr aa REACHABLE\n", "")
        with patch("music_libraries.subprocess.run", return_value=result):
            hosts = default_discovery_hosts()

        self.assertIn("openclaw", hosts)
        self.assertIn("192.168.178.100", hosts)

    def test_configured_discovery_hosts_skip_neighbor_scan(self):
        with patch.dict("os.environ", {"MUSIC_LIBRARY_SMB_HOSTS": "OpenClaw@192.168.178.100"}):
            with patch("music_libraries.subprocess.run") as run:
                hosts = default_discovery_hosts()

        self.assertEqual(hosts, ["OpenClaw@192.168.178.100"])
        run.assert_not_called()

    def test_discovers_typed_smb_shares_and_filters_system_shares(self):
        listing = subprocess.CompletedProcess(
            [], 0, "Disk|Music-Demo|Demo library\nDisk|print$|Drivers\nIPC|IPC$|IPC\n", ""
        )
        readable = subprocess.CompletedProcess([], 0, "  album D 0\n", "")
        with patch("music_libraries.subprocess.run", side_effect=[listing, readable]):
            shares = discover_smb_shares(["openclaw"])

        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0]["type"], "smb")
        self.assertEqual(shares[0]["label"], "SMB — OpenClaw / Music-Demo")
        self.assertEqual(shares[0]["server"], "openclaw")
        self.assertEqual(shares[0]["share"], "Music-Demo")

    def test_failed_hosts_do_not_break_discovery(self):
        failed = subprocess.CompletedProcess([], 1, "", "unavailable")
        with patch("music_libraries.subprocess.run", return_value=failed):
            self.assertEqual(discover_smb_shares(["offline"]), [])

    def test_labeled_host_uses_friendly_server_name(self):
        listing = subprocess.CompletedProcess([], 0, "Disk|Music-Demo|Demo library\n", "")
        readable = subprocess.CompletedProcess([], 0, "  album D 0\n", "")
        with patch("music_libraries.subprocess.run", side_effect=[listing, readable]) as run:
            shares = discover_smb_shares(["OpenClaw@192.168.178.100"])

        self.assertEqual(run.call_args_list[0].args[0][-1], "//192.168.178.100")
        self.assertEqual(shares[0]["label"], "SMB — OpenClaw / Music-Demo")
        self.assertEqual(shares[0]["id"], "smb:192.168.178.100:Music-Demo")

    def test_unreadable_share_is_not_offered(self):
        listing = subprocess.CompletedProcess([], 0, "Disk|Private|\n", "")
        denied = subprocess.CompletedProcess([], 1, "", "NT_STATUS_ACCESS_DENIED")
        with patch("music_libraries.subprocess.run", side_effect=[listing, denied]):
            self.assertEqual(discover_smb_shares(["server"]), [])


class MusicLibraryManagerTests(unittest.TestCase):
    def test_local_entry_uses_only_folder_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Private" / "Music"
            root.mkdir(parents=True)
            manager = MusicLibraryManager(root, discovery_hosts=[])

            entry = manager.list_libraries()[0]

            self.assertEqual(entry["type"], "local")
            self.assertEqual(entry["label"], "Local — Music")
            self.assertNotIn(str(root.parent), entry["label"])

    def test_manual_smb_entry_remains_available(self):
        with tempfile.TemporaryDirectory() as td:
            manager = MusicLibraryManager(Path(td), discovery_hosts=[])
            entries = manager.list_libraries()

            self.assertEqual(entries[-1], {
                "id": "manual",
                "type": "action",
                "label": "Add network share manually…",
            })

    def test_smb_activation_uses_existing_mount_and_local_restores_root(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            local = base / "Music"
            mounted = base / "mounts" / "openclaw" / "Music-Demo"
            local.mkdir()
            mounted.mkdir(parents=True)
            manager = MusicLibraryManager(local, mount_root=base / "mounts", discovery_hosts=[])
            manager.add_manual_share("openclaw", "Music-Demo")

            smb_root = manager.activate("smb:openclaw:Music-Demo")
            local_root = manager.activate("local")

            self.assertEqual(smb_root, mounted.resolve())
            self.assertEqual(manager.active_type, "local")
            self.assertEqual(local_root, local.resolve())

    def test_manual_entry_accepts_smb_url(self):
        with tempfile.TemporaryDirectory() as td:
            manager = MusicLibraryManager(Path(td), discovery_hosts=[])
            entry = manager.add_manual_url("smb://openclaw/Music-Demo")

            self.assertEqual(entry["id"], "smb:openclaw:Music-Demo")
            self.assertEqual(entry["type"], "smb")

    def test_structure_leaves_room_for_other_library_types(self):
        with tempfile.TemporaryDirectory() as td:
            manager = MusicLibraryManager(Path(td), discovery_hosts=[])
            with self.assertRaisesRegex(ValueError, "Unsupported music library type"):
                manager.activate("nfs:server:music")

    def test_unlisted_smb_id_cannot_select_an_arbitrary_directory(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            target = base / "mounts" / "server" / "share"
            target.mkdir(parents=True)
            manager = MusicLibraryManager(base / "Music", mount_root=base / "mounts", discovery_hosts=[])

            with self.assertRaisesRegex(ValueError, "Unknown music library"):
                manager.activate("smb:server:share")

    def test_registered_share_cannot_escape_mount_root(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            manager = MusicLibraryManager(base / "Music", mount_root=base / "mounts", discovery_hosts=[])
            with self.assertRaisesRegex(ValueError, "Invalid SMB"):
                manager.add_manual_share("server", "..")

    def test_discovery_results_are_cached(self):
        with tempfile.TemporaryDirectory() as td:
            manager = MusicLibraryManager(Path(td), discovery_hosts=["openclaw"])
            with patch("music_libraries.discover_smb_shares", return_value=[]) as discover:
                manager.list_libraries()
                manager.list_libraries()

            discover.assert_called_once()


if __name__ == "__main__":
    unittest.main()
