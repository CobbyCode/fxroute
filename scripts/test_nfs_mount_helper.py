#!/usr/bin/env python3
"""Exercise the real helper with isolated fstab and command boundaries."""

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NfsMountHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fstab = self.root / "fstab"
        self.fstab.write_text("# existing\n//nas/Music /old cifs guest 0 0 # fxroute-managed\n")
        self.original = self.fstab.read_text()
        self.log = self.root / "commands"
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        commands = {
            "id": '#!/bin/sh\necho 1000\n',
            "getent": '#!/bin/sh\necho tester:x:1000:1000::/home/tester:/bin/sh\n',
            "systemd-escape": '#!/bin/sh\necho test-mount\n',
            "systemctl": '#!/bin/sh\necho "$*" >> "$COMMAND_LOG"\ncase "$1" in show) echo "${UNIT_STATE:-inactive}";; stop) [ "${FAIL_STOP:-0}" != 1 ];; esac\n',
            "install": '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do case "$1" in -o|-g|-m) shift 2;; -d) shift;; *) mkdir -p "$1"; shift;; esac; done\n',
            "findmnt": '#!/bin/sh\nexit 1\n',
            "mount": '#!/bin/bash\necho "$*" >> "$COMMAND_LOG"\nmkdir -p "${@: -1}/Music"\n',
            "umount": '#!/bin/sh\necho "$*" >> "$COMMAND_LOG"\n',
        }
        for name, source in commands.items():
            target = fake_bin / name
            target.write_text(source)
            target.chmod(0o755)
        source = (ROOT / "scripts/fxroute-cifs-mount").read_text()
        source = (source.replace("/etc/fstab", str(self.fstab))
                  .replace("/run/lock/fxroute-cifs-mount.lock", str(self.root / "mount.lock"))
                  .replace("/var/lib/fxroute", str(self.root / "var/lib/fxroute"))
                  .replace("/run/fxroute-nfs-discovery", str(self.root / "probe"))
                  .replace('PATH="/usr/sbin:/usr/bin:/sbin:/bin"', f'PATH="{fake_bin}:{os.environ["PATH"]}"')
                  .replace('[[ $EUID -eq 0 &&', '[[ 0 -eq 0 &&'))
        self.helper = self.root / "helper"
        self.helper.write_text(source)
        self.env = {**os.environ, "SUDO_USER": "tester", "COMMAND_LOG": str(self.log)}

    def run_helper(self, *args, **env):
        return subprocess.run(["bash", str(self.helper), *args], env={**self.env, **env},
                              capture_output=True, text=True, timeout=10)

    def mount_point(self, export="/exports/My Music"):
        return self.root / "var/lib/fxroute/music-libraries/1000-nfs/nas" / hashlib.sha256(export.encode()).hexdigest()

    def test_nfs_mount_is_idempotent_readonly_and_boot_safe(self):
        for _ in range(2):
            result = self.run_helper("--nfs", "nas", "/exports/My Music")
            self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in self.fstab.read_text().splitlines() if line.startswith("nas:")]
        self.assertEqual(len(lines), 1)
        source, mount, fs, options, *_ = lines[0].split()
        self.assertEqual(source, "nas:/exports/My\\040Music")
        self.assertEqual(mount, str(self.mount_point()))
        self.assertEqual(fs, "nfs")
        for option in ("ro", "soft", "timeo=20", "retrans=2", "retry=0", "nosuid", "nodev", "noexec",
                       "_netdev", "nofail", "x-systemd.automount", "x-systemd.mount-timeout=10s"):
            self.assertIn(option, options.split(","))
        self.assertNotIn("vers=", options)
        self.assertIn(self.original, self.fstab.read_text())
        self.assertIn("reset-failed test-mount.mount", self.log.read_text())
        self.assertIn("start test-mount.mount", self.log.read_text())

    def test_nfs_rejects_unsafe_inputs_without_modifying_fstab(self):
        for server, export in (("--remove-all", "/music"), ("-o", "/music"), ("nas", "relative"),
                               ("nas", "/a/../b"), ("nas", "/a/./b"), ("nas", "/a,b"),
                               ("nas", "/a\ncomment"), ("nas", "/a\\040b")):
            with self.subTest(server=server, export=export):
                result = self.run_helper("--nfs", server, export)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.fstab.read_text(), self.original)

    def test_nfs_refuses_symlink_mount_parent(self):
        parent = self.mount_point().parent
        parent.parent.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        parent.symlink_to(outside, target_is_directory=True)
        result = self.run_helper("--nfs", "nas", "/exports/My Music")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.fstab.read_text(), self.original)
        self.assertEqual(list(outside.iterdir()), [])

    def test_targeted_removal_keeps_smb_and_foreign_entries(self):
        result = self.run_helper("--nfs", "nas", "/exports/My Music")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_helper("--remove", "nfs", "nas", "/exports/My Music")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fstab.read_text(), self.original)

    def test_failed_stop_keeps_fstab_for_retry(self):
        result = self.run_helper("--nfs", "nas", "/exports/My Music")
        self.assertEqual(result.returncode, 0, result.stderr)
        before = self.fstab.read_text()
        result = self.run_helper("--remove", "nfs", "nas", "/exports/My Music", UNIT_STATE="active", FAIL_STOP="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.fstab.read_text(), before)

    def test_remove_all_cleans_both_protocols_only_under_owned_root(self):
        for args in (("--nfs", "nas", "/exports/My Music"), ("nas", "Music")):
            result = self.run_helper(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_helper("--remove-all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fstab.read_text(), self.original)

    def test_foreign_mount_cannot_be_replaced_or_removed(self):
        foreign = f"other:/music {self.mount_point()} nfs ro 0 0\n"
        self.fstab.write_text(self.original + foreign)
        for args in (("--nfs", "nas", "/exports/My Music"),
                     ("--remove", "nfs", "nas", "/exports/My Music"), ("--remove-all",)):
            result = self.run_helper(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.fstab.read_text(), self.original + foreign)

    def test_nfs4_probe_lists_namespace_without_persisting_mount(self):
        result = self.run_helper("--discover-nfs", "nas")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["/", "/Music"])
        self.assertEqual(self.fstab.read_text(), self.original)
        self.assertIn("vers=4", self.log.read_text())
        self.assertIn("-l", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
