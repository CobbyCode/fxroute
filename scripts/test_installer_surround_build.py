#!/usr/bin/env python3
"""Exercise the installer's Surround build dispatch without installing packages."""

import os
import pwd
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def installer_function(name):
    text = (ROOT / "install.sh").read_text()
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}", text, re.M | re.S)
    if not match:
        raise AssertionError(f"Missing installer function: {name}")
    return match.group()


class SurroundBuildInstallerTests(unittest.TestCase):
    def run_build(self, script):
        directory = tempfile.TemporaryDirectory(prefix="fxroute-surround-install-")
        self.addCleanup(directory.cleanup)
        home = Path(directory.name) / "home"
        home.mkdir()
        install = home / "FXRoute build's source"
        native = install / "native_surround"
        native.mkdir(parents=True)
        if script is not None:
            (native / "build.sh").write_text(script)
        functions = "\n".join(installer_function(name) for name in (
            "run_as_target_user", "path_has_symlink_component",
            "prepare_target_user_directory", "build_native_surround_input",
        ))
        harness = r'''
set -Eeuo pipefail
INSTALL_ROOT="$1"
FXROUTE_TARGET_HOME="$2"
FXROUTE_TARGET_USER="$3"
FXROUTE_RUNTIME_DIR="$2/runtime"
PACKAGE_MANAGER=apt
pkg_install() { :; }
die() { printf '%s\n' "$*" >&2; exit 1; }
pass() { :; }
'''
        result = subprocess.run(
            ["bash", "-c", harness + functions + "\nbuild_native_surround_input\n",
             "surround-installer-test", str(install), str(home), pwd.getpwuid(os.getuid()).pw_name],
            capture_output=True, text=True, timeout=15,
        )
        return result, home, native

    def test_build_produces_executable_with_target_home_and_quoted_path(self):
        result, home, native = self.run_build(r'''#!/bin/sh
set -eu
cd "$(dirname "$0")"
printf '%s\n' "$HOME" > build/target-home
printf '%s\n' '#!/bin/sh' 'printf "surround-ready\n"' > build/fxroute-surround-input
chmod +x build/fxroute-surround-input
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((native / "build/target-home").read_text(), f"{home}\n")
        output = subprocess.check_output([str(native / "build/fxroute-surround-input")], text=True)
        self.assertEqual(output, "surround-ready\n")

    def test_failed_build_aborts_installation(self):
        result, _, native = self.run_build("#!/bin/sh\nexit 23\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Surround input build failed", result.stderr)
        self.assertFalse((native / "build/fxroute-surround-input").exists())

    def test_success_without_binary_aborts_installation(self):
        result, _, _ = self.run_build("#!/bin/sh\nexit 0\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Surround input binary missing after build", result.stderr)

    def test_missing_build_script_aborts_installation(self):
        result, _, _ = self.run_build(None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing Surround input build script", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
