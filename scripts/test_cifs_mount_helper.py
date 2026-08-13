#!/usr/bin/env python3
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "fxroute-cifs-mount"


class CifsMountHelperTests(unittest.TestCase):
    def test_rejects_unsafe_server_and_share_before_mutation(self):
        text = HELPER.read_text()
        self.assertLess(text.index('[[ "$server" =~'), text.index("install -d"))
        self.assertLess(text.index('share in {".", ".."}') if 'share in {".", ".."}' in text else text.index('"$share" != ".."'), text.index("install -d"))
        self.assertIn('if [[ "$server" == "--remove-all" ]]', text)
        self.assertIn("systemctl stop", text)

    def test_fstab_update_is_idempotent_and_uses_safe_options(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_fstab = root / "fstab"
            fake_fstab.write_text("# test fstab\n")
            command_log = root / "commands.log"

            scripts = {
                "getent": '#!/bin/sh\nprintf "tester:x:1000:1000::%s/home:/bin/sh\\n" "$TEST_ROOT"\n',
                "id": '#!/bin/sh\n[ "$1" = -u ] && echo 1000 || echo 1000\n',
                "systemd-escape": '#!/bin/sh\necho test-mount\n',
                "systemctl": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$COMMAND_LOG"\n',
                "install": '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do case "$1" in -o|-g|-m) shift 2;; -d) shift;; *) mkdir -p "$1"; shift;; esac; done\n',
            }
            for name, content in scripts.items():
                path = fake_bin / name
                path.write_text(content)
                path.chmod(0o755)

            helper = root / "helper"
            source = (
                HELPER.read_text()
                .replace("/etc/fstab", str(fake_fstab))
                .replace("/run/lock/fxroute-cifs-mount.lock", str(root / "mount.lock"))
                .replace('PATH="/usr/sbin:/usr/bin:/sbin:/bin"', f'PATH="{fake_bin}:{os.environ["PATH"]}"')
                .replace("/var/lib/fxroute", str(root / "var" / "lib" / "fxroute"))
            )
            helper.write_text(source)
            helper.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "SUDO_USER": "tester",
                "TEST_ROOT": str(root),
                "COMMAND_LOG": str(command_log),
            }
            # The production script requires EUID 0; CI runs this structural
            # behavior test through bash after replacing only that guard.
            source = helper.read_text().replace('[[ $EUID -eq 0 &&', '[[ 0 -eq 0 &&')
            helper.write_text(source)
            subprocess.run([str(helper), "server", "My Music"], env=env, check=True)
            subprocess.run([str(helper), "server", "My Music"], env=env, check=True)

            lines = [line for line in fake_fstab.read_text().splitlines() if line.startswith("//server/My\\040Music ")]
            self.assertEqual(len(lines), 1)
            for option in ("_netdev", "nofail", "x-systemd.automount", "x-systemd.mount-timeout=10s"):
                self.assertIn(option, lines[0])
            self.assertIn("daemon-reload", command_log.read_text())
            self.assertIn("start test-mount.automount", command_log.read_text())


if __name__ == "__main__":
    unittest.main()
