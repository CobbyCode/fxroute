#!/usr/bin/env python3
"""Artifact regressions: FXROUTE_TEST_LIVE_SQUASH=/path/to/squashfs.img.

These inspect the packaged filesystem, not strings in the builder. Run with
SOURCE_DATE_EPOCH=0 when building the fixture to exercise the SDDM regression.
"""
import os
import re
import subprocess
import unittest


@unittest.skipUnless(os.environ.get("FXROUTE_TEST_LIVE_SQUASH"), "requires a built live SquashFS")
class LiveRootImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = os.environ["FXROUTE_TEST_LIVE_SQUASH"]
        listing = subprocess.check_output(
            ["unsquashfs", "-lln", "-full-precision", "-UTC", cls.image], text=True
        )
        cls.files = {}
        for line in listing.splitlines():
            match = re.match(
                r"(\S+)\s+(\d+)/(\d+)\s+\d+\s+(\S+)\s+(\S+)\s+squashfs-root(.*)", line
            )
            if match:
                mode, uid, gid, date, time, path = match.groups()
                cls.files[path.split(" -> ")[0] or "/"] = (mode, int(uid), int(gid), date, time)
        passwd = cls.cat("/etc/passwd")
        cls.users = {row.split(":")[0]: row.split(":") for row in passwd.splitlines()}

    @classmethod
    def cat(cls, path):
        return subprocess.check_output(["unsquashfs", "-cat", cls.image, path], text=True)

    def test_system_and_session_directories_keep_their_owners(self):
        for path in ("/", "/etc", "/usr", "/etc/shadow", "/etc/sddm.conf.d/10-fxroute-autologin.conf"):
            with self.subTest(path=path):
                self.assertEqual(self.files[path][1], 0, "system file was reassigned to the build user")
        for user, path in (("sddm", "/var/lib/sddm"), ("fxroute", "/home/fxroute")):
            with self.subTest(user=user):
                self.assertEqual(self.files[path][1:3], tuple(map(int, self.users[user][2:4])))

    def test_export_preserves_privileged_binary_permissions(self):
        self.assertEqual(self.files["/usr/bin/sudo"][1], 0)
        self.assertEqual(self.files["/usr/bin/sudo"][0][3], "s", "export stripped setuid")

    def test_sddm_configuration_has_nonzero_mtime(self):
        for path in ("/etc/sddm.conf.d/10-fxroute-autologin.conf", "/usr/lib/sddm/sddm.conf.d/00-general.conf"):
            with self.subTest(path=path):
                self.assertGreater(self.files[path][3:5], ("1970-01-01", "00:00:00"),
                                   "SDDM skips epoch-zero configuration and uses a missing Xsession path")

    def test_live_root_is_not_identified_as_a_container(self):
        for path in ("/.dockerenv", "/run/.containerenv", "/run/systemd/container"):
            with self.subTest(path=path):
                self.assertFalse(path in self.files, f"container marker packaged: {path}")

    def test_display_manager_account_does_not_require_password_change(self):
        accounts = {row.split(":")[0]: row.split(":") for row in self.cat("/etc/shadow").splitlines()}
        self.assertNotEqual(accounts["sddm"][2], "0", "epoch day zero makes PAM reject the SDDM user manager")

    def test_suse_autologin_and_vendor_session_are_available(self):
        config = self.cat("/etc/sysconfig/displaymanager")
        autologin = re.search(r'(?m)^DISPLAYMANAGER_AUTOLOGIN="(.*)"$', config)
        self.assertIsNotNone(autologin)
        self.assertEqual(autologin.group(1), "fxroute")
        config = self.cat("/usr/lib/sddm/sddm.conf.d/00-general.conf")
        session = re.search(r"(?m)^SessionCommand=(.+)$", config).group(1)
        self.assertIn(session, self.files)
        self.assertIn("x", self.files[session][0], "SDDM's vendor session wrapper must be executable")


if __name__ == "__main__":
    unittest.main()
