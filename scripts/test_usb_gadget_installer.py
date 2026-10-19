#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Installer capability gate reuses the real gadget hardware probe."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_usb_gadget_system as gadget_tests


class InstallerTests(unittest.TestCase):
    setUp = gadget_tests.SystemTests.setUp
    put = gadget_tests.SystemTests.put
    udc = gadget_tests.SystemTests.udc
    pi_boot = gadget_tests.SystemTests.pi_boot
    run_command = gadget_tests.SystemTests.run_command

    def check_support(self, *, shadow_json=False):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            (stage / 'scripts').mkdir()
            (stage / 'audio').mkdir()
            installer = stage / 'scripts/install_usb_gadget.sh'
            installer.write_bytes((source / 'scripts/install_usb_gadget.sh').read_bytes())
            # Only replace the system boundary: use the actual capability policy.
            (stage / 'audio/usb_gadget_system.py').write_text(
                'import importlib.util, json\nfrom pathlib import Path\n'
                f'spec = importlib.util.spec_from_file_location("gadget", {str(source / "audio/usb_gadget_system.py")!r})\n'
                'module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n'
                f'system = module.GadgetSystem(Path({str(self.root)!r}), run=lambda args: "")\n'
                'print(json.dumps(system.status()))\n')
            if shadow_json:
                (stage / 'json.py').write_text('raise RuntimeError("Unexpected local import")\n')
            return subprocess.run(['bash', str(installer), '--check-support'], cwd=stage,
                                  capture_output=True, text=True, timeout=5)

    def test_privileged_capability_parser_ignores_caller_python_modules(self):
        self.udc()
        result = self.check_support(shadow_json=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_rejects_no_controller_without_privileged_writes(self):
        result = self.check_support()
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_installer_accepts_generic_udc(self):
        self.udc('musb-hdrc.0')
        result = self.check_support()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_keeps_supported_pi_boot_preparation(self):
        config = self.pi_boot()
        before = config.read_bytes()
        result = self.check_support()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
