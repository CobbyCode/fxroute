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
