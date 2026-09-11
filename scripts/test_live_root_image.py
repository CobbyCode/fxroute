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

    def test_live_root_ships_pointer_driver_modules(self):
        hid = [p for p in self.files if re.search(r"/modules/[^/]+/kernel/drivers/hid/usbhid/usbhid\.ko(\.\w+)?$", p)]
        self.assertTrue(hid, "live root has no usbhid module; USB mice can never bind")
        i2c_hid = [p for p in self.files if re.search(r"/modules/[^/]+/kernel/drivers/hid/i2c-hid/i2c-hid\.ko(\.\w+)?$", p)]
        self.assertTrue(i2c_hid, "live root has no i2c-hid module; I2C trackpads can never bind")
        dep = [p for p in self.files if re.search(r"/modules/[^/]+/modules\.dep$", p)]
        self.assertTrue(dep, "live root has no modules.dep; on-demand driver loading cannot resolve aliases")

    def test_live_root_ships_wifi_driver_modules(self):
        for driver in ("net/wireless/intel/iwlwifi/iwlwifi",
                       "net/wireless/ath/ath11k/ath11k",
                       "net/wireless/mediatek/mt76/mt7921/mt7921e",
                       "net/wireless/realtek/rtw89/rtw89_8852ae"):
            with self.subTest(driver=driver):
                found = [p for p in self.files
                         if re.search(r"/modules/[^/]+/kernel/drivers/%s\.ko(\.\w+)?$" % driver, p)]
                self.assertTrue(found, f"live root has no {driver} module")

    def test_live_root_ships_wifi_firmware(self):
        # Leap 16 compresses firmware (.bin.xz/.ucode.xz); match any suffix.
        for pattern, hint in ((r"/usr/lib/firmware/iwlwifi-[^/]+\.ucode(\.\w+)?$", "iwlwifi"),
                              (r"/usr/lib/firmware/ath11k/.+\.bin(\.\w+)?$", "ath11k"),
                              (r"/usr/lib/firmware/mediatek/.+", "mediatek"),
                              (r"/usr/lib/firmware/rtw89/[^/]+\.bin(\.\w+)?$", "rtw89"),
                              (r"/usr/lib/firmware/regulatory\.db$", "regdb")):
            with self.subTest(firmware=hint):
                found = [p for p in self.files if re.search(pattern, p)]
                self.assertTrue(found, f"live root has no {hint} firmware")

    def test_live_desktop_links_match_installed_desktop(self):
        for name in ("/home/fxroute/Desktop/FXRoute.desktop",
                     "/home/fxroute/Desktop/Spotify Download.desktop"):
            with self.subTest(link=name):
                self.assertIn(name, self.files)
        fxroute_link = self.cat("/home/fxroute/Desktop/FXRoute.desktop")
        self.assertIn("http://127.0.0.1:8000/", fxroute_link)
        self.assertIn("/usr/share/pixmaps/fxroute.svg", fxroute_link)
        self.assertIn("/usr/local/libexec/fxroute-appliance-session-init.sh", self.files)

    def test_live_networkmanager_starts_at_boot(self):
        self.assertIn("/etc/systemd/system/multi-user.target.wants/NetworkManager.service", self.files,
                      "NetworkManager is not enabled; no interface comes up and Plasma lists no WLANs")

    def test_live_user_is_in_input_group(self):
        groups = {}
        for row in self.cat("/etc/group").splitlines():
            parts = row.split(":")
            groups[parts[0]] = parts[3].split(",") if len(parts) > 3 and parts[3] else []
        self.assertIn("fxroute", groups.get("input", []))

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
