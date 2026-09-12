#!/usr/bin/env python3
"""Live-from-installed converter contract and fixture tests."""
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONVERTER = ROOT / "iso" / "scripts" / "build-live-from-installed.sh"


def run_filter(script_call, stdin_text):
    proc = subprocess.run(
        ["bash", "-c", script_call], input=stdin_text,
        capture_output=True, text=True, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class LiveFromInstallTests(unittest.TestCase):
    def test_converter_exists_and_takes_disk_and_output(self):
        text = CONVERTER.read_text(encoding="utf-8")
        self.assertIn("--disk", text)
        self.assertIn("--output", text)
        self.assertIn("--base-iso", text)

    def test_converter_neutralizes_disk_backed_fstab_entries(self):
        fixture = (
            "# comment stays\n"
            "UUID=abc-123 / btrfs defaults 0 0\n"
            "UUID=abc-123 /home btrfs subvol=/@home 0 0\n"
            "LABEL=swap swap swap defaults 0 0\n"
            "/dev/vda1 /boot/efi vfat defaults 0 0\n"
            "tmpfs /dev/shm tmpfs defaults 0 0\n"
            "devpts /dev/pts devpts mode=0620 0 0\n"
        )
        out = run_filter(
            "source iso/scripts/build-live-from-installed.sh --source-only 2>/dev/null; filter_fstab",
            fixture,
        )
        self.assertIn("# comment stays", out)
        self.assertIn("tmpfs /dev/shm", out)
        self.assertIn("devpts /dev/pts", out)
        self.assertNotIn("UUID=", out)
        self.assertNotIn("LABEL=", out)
        self.assertNotIn("/dev/vda1", out)

    def test_converter_scrubs_credentials_and_identity(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td)
            (tree / "etc/ssh").mkdir(parents=True)
            (tree / "etc/ssh/ssh_host_key").write_text("PRIVATE")
            nm = tree / "etc/NetworkManager/system-connections"
            nm.mkdir(parents=True)
            (nm / "wifi.nmconnection").write_text("[wifi]\npsk=secret\n")
            (tree / "etc").mkdir(exist_ok=True)
            (tree / "etc/machine-id").write_text("old-id\n")
            (tree / "etc/hostname").write_text("old-name\n")
            (tree / "etc/fstab").write_text("UUID=abc / btrfs defaults 0 0\ntmpfs /dev/shm tmpfs defaults 0 0\n")
            (tree / "etc/systemd/system").mkdir(parents=True)
            (tree / "etc/systemd/system/multi-user.target.wants").mkdir(parents=True)
            (tree / "etc/systemd/system/multi-user.target.wants/sshd.service").symlink_to(
                "/usr/lib/systemd/system/sshd.service")
            (tree / "root").mkdir(exist_ok=True)
            (tree / "root/.bash_history").write_text("secret-cmd\n")
            iso_state = tree / "var/lib/fxroute-iso"
            iso_state.mkdir(parents=True)
            (iso_state / "install-complete").write_text("done\n")
            proc = subprocess.run(
                ["bash", "-c",
                 "source iso/scripts/build-live-from-installed.sh --source-only 2>/dev/null;"
                 f" LIVE_TREE='{tree}' BUILD_COMMIT=test scrub_tree"],
                capture_output=True, text=True, cwd=ROOT,
            )
            assert proc.returncode == 0, proc.stderr
            self.assertFalse((tree / "etc/ssh/ssh_host_key").exists())
            self.assertFalse((nm / "wifi.nmconnection").exists())
            self.assertEqual((tree / "etc/machine-id").read_text(), "")
            self.assertEqual((tree / "etc/hostname").read_text(), "fxroute-live\n")
            self.assertFalse((tree / "root/.bash_history").exists())
            self.assertFalse((tree / "etc/systemd/system/multi-user.target.wants/sshd.service").exists())
            self.assertFalse((iso_state / "install-complete").exists())
            self.assertTrue((tree / "etc/fxroute-live").is_file())
            fstab = (tree / "etc/fstab").read_text()
            self.assertNotIn("UUID=", fstab)
            self.assertIn("tmpfs /dev/shm", fstab)

    def _conversion_fixture(self, tree):
        """Minimal extracted-system tree with two regular accounts."""
        (tree / "etc").mkdir()
        (tree / "etc/passwd").write_text(
            "root:x:0:0:root:/root:/bin/bash\n"
            "sddm:x:471:471:Display Manager:/var/lib/sddm:/sbin/nologin\n"
            "fxroute:x:1000:100:FXRoute:/home/fxroute:/bin/bash\n"
            "other:x:1001:100:Other:/home/other:/bin/bash\n"
        )
        (tree / "etc/shadow").write_text(
            "root:$6$root-hash:19000:0:99999:7:::\n"
            "sddm:!:19000:0:99999:7:::\n"
            "fxroute:$6$live-hash:19000:0:99999:7:::\n"
            "other:$6$other-hash:19000:0:99999:7:::\n"
        )
        (tree / "etc/fstab").write_text("UUID=abc / btrfs defaults 0 0\n")
        (tree / "etc/systemd/system/multi-user.target.wants").mkdir(parents=True)
        for account in ("fxroute", "other"):
            home = tree / "home" / account
            (home / ".config/fxroute").mkdir(parents=True)
            (home / ".config/fxroute/install-state.json").write_text("{}")
            (home / ".local/share/fxroute").mkdir(parents=True)
            (home / ".local/share/fxroute/appliance-ready").write_text("")
            (home / ".config/spotifyd").mkdir(parents=True)
            (home / ".config/spotifyd/spotifyd.conf").write_text("username=reference\n")
            (home / ".mozilla/firefox/profile").mkdir(parents=True)
            (home / ".mozilla/firefox/profile/places.sqlite").write_text("history")
            (home / "Music").mkdir()
            (home / "Music/album.flac").write_text("audio")
            (home / ".bash_history").write_text("secret-cmd\n")
        # Runtime configuration the live session needs must survive the scrub.
        live = tree / "home/fxroute"
        (live / ".lv2/calf.lv2").mkdir(parents=True)
        (live / ".config/wireplumber").mkdir(parents=True)
        (live / "Desktop").mkdir()
        (live / "Desktop/FXRoute.desktop").write_text("[Desktop Entry]\n")

    def _run_scrub(self, tree, live_user="fxroute"):
        return subprocess.run(
            ["bash", "-c",
             "source iso/scripts/build-live-from-installed.sh --source-only 2>/dev/null;"
             f" LIVE_TREE='{tree}' BUILD_COMMIT=test LIVE_USER={live_user} scrub_tree"],
            capture_output=True, text=True, cwd=ROOT,
        )

    def test_converter_scrubs_password_hashes_of_every_account(self):
        """No account hash of the reference system may reach the live image."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td)
            self._conversion_fixture(tree)
            proc = self._run_scrub(tree)
            assert proc.returncode == 0, proc.stderr
            shadow = {
                row.split(":")[0]: row.split(":")
                for row in (tree / "etc/shadow").read_text().splitlines()
            }
            # The live account logs in without a password; every other account
            # loses its hash material but keeps the rest of its shadow line.
            self.assertEqual(shadow["fxroute"][1], "")
            self.assertEqual(shadow["other"][1], "!")
            self.assertEqual(shadow["root"][1], "!")
            self.assertEqual(shadow["sddm"][1], "!")
            self.assertEqual(shadow["fxroute"][2], "19000", "last-change day must survive")
            # sudo and the live init are bound to the resolved account.
            self.assertEqual(
                (tree / "etc/sudoers.d/99-fxroute-live").read_text(),
                "fxroute ALL=(ALL) NOPASSWD:ALL\n",
            )
            self.assertEqual((tree / "etc/fxroute-live-user").read_text(), "fxroute\n")

    def test_converter_scrubs_reference_user_state_but_keeps_live_runtime(self):
        """Reference installer/user state must not ship; live runtime must."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td)
            self._conversion_fixture(tree)
            proc = self._run_scrub(tree)
            assert proc.returncode == 0, proc.stderr
            for account in ("fxroute", "other"):
                home = tree / "home" / account
                for gone in (".config/fxroute", ".local/share/fxroute", ".config/spotifyd",
                             ".mozilla/firefox/profile/places.sqlite", "Music/album.flac",
                             ".bash_history"):
                    with self.subTest(account=account, path=gone):
                        self.assertFalse((home / gone).exists())
            live = tree / "home/fxroute"
            for kept in (".lv2/calf.lv2", ".config/wireplumber", "Desktop/FXRoute.desktop"):
                with self.subTest(path=kept):
                    self.assertTrue((live / kept).exists())

    def test_converter_scrubs_firefox_session_state_including_directories(self):
        """Firefox session state ships neither as files nor as directories.

        Regression: sessionstore-backups/ is a directory, and `rm -f`
        refuses directories, which aborted the conversion under `set -e`.
        """
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td)
            self._conversion_fixture(tree)
            profile = tree / "home/fxroute/.mozilla/firefox/profile"
            (profile / "sessionstore-backups").mkdir()
            (profile / "sessionstore-backups/recovery.jsonlz4").write_text("session")
            (profile / "sessionstore.jsonlz4").write_text("session")
            proc = subprocess.run(
                ["bash", "-c",
                 "set -Eeuo pipefail;"
                 "source iso/scripts/build-live-from-installed.sh --source-only 2>/dev/null;"
                 f" LIVE_TREE='{tree}' BUILD_COMMIT=test LIVE_USER=fxroute scrub_tree"],
                capture_output=True, text=True, cwd=ROOT,
            )
            assert proc.returncode == 0, proc.stderr
            self.assertFalse((profile / "sessionstore-backups").exists())
            self.assertFalse((profile / "sessionstore.jsonlz4").exists())
            # The profile skeleton itself stays usable for the kiosk.
            self.assertTrue(profile.is_dir())

    def test_converter_refuses_a_live_account_that_is_not_in_the_system(self):
        """A mismatched --ssh-user must fail loudly, never leak credentials."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tree = Path(td)
            self._conversion_fixture(tree)
            proc = self._run_scrub(tree, live_user="missing")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("not a regular account", proc.stderr)
            # The failed conversion must leave the tree untouched.
            self.assertIn("$6$live-hash", (tree / "etc/shadow").read_text())
            self.assertFalse((tree / "etc/sudoers.d/99-fxroute-live").exists())

    def test_converter_masks_first_boot_and_keeps_iso_kernel_modules(self):
        text = CONVERTER.read_text(encoding="utf-8")
        self.assertIn("fxroute-first-boot.service", text)
        self.assertIn("/dev/null", text)
        self.assertIn("kernel-default-extra", text)
        self.assertIn("depmod", text)

    def test_converter_help_works_without_env(self):
        environment = {
            key: value for key, value in os.environ.items()
            if key != "FXROUTE_LIVE_CONVERT_PASSWORD"
        }
        proc = subprocess.run(
            ["bash", str(CONVERTER), "--help"],
            capture_output=True, text=True, cwd=ROOT, env=environment,
        )
        assert proc.returncode == 0, proc.stderr
        assert "Usage:" in proc.stdout

    def test_converter_packs_as_root_and_keeps_host_artifact_ownership(self):
        # The extracted tree is root-owned. The pack step must run inside the
        # (root) container so system owners, service-account directories and
        # setuid bits survive even when the host build runs as a non-root
        # user; only the finished image is handed back to the host user.
        text = CONVERTER.read_text(encoding="utf-8")
        self.assertIn("mksquashfs /t /o/squashfs.img", text)
        self.assertIn("tar -x -C /t --numeric-owner", text)
        self.assertIn('chown "$HOST_UID:$HOST_GID" /o/squashfs.img', text)

    def test_iso_builder_calls_new_converter(self):
        text = (ROOT / "iso" / "build-leap-16-iso.sh").read_text(encoding="utf-8")
        self.assertIn("build-live-from-installed", text)
        self.assertNotIn("build-live-root.sh", text)


if __name__ == "__main__":
    unittest.main()
