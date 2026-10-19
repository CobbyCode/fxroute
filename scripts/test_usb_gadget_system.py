#!/usr/bin/env python3
"""USB gadget lifecycle against a simulated kernel filesystem."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio import usb_gadget_system as gadget


class KernelConfigFS(gadget.ConfigFS):
    """Model kernel-created attributes and ConfigFS rmdir semantics."""
    fail_bind = False

    def mkdir(self, path):
        super().mkdir(path)
        if path.name == gadget.GADGET_NAME:
            for directory in ('functions', 'configs', 'strings', 'os_desc', 'webusb'):
                (path / directory).mkdir()
            for attribute in ('UDC', 'idVendor', 'idProduct', 'bcdUSB', 'bcdDevice'):
                (path / attribute).write_text('')
        elif path.name == 'uac2.usb0':
            for attribute in ('c_chmask', 'p_chmask', 'c_srate', 'c_ssize'):
                (path / attribute).write_text('')
        elif path.name == 'c.1':
            (path / 'strings').mkdir()
            for attribute in ('MaxPower', 'bmAttributes'):
                (path / attribute).write_text('')
        elif path.name == '0x409':
            for attribute in ('manufacturer', 'product', 'serialnumber', 'configuration'):
                (path / attribute).write_text('')

    def write(self, path, value):
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.name == 'UDC' and value.strip() and self.fail_bind:
            raise OSError('UDC busy')
        super().write(path, value)

    def rmdir(self, path):
        for child in list(path.iterdir()):
            if child.is_file() and not child.is_symlink():
                child.unlink()
            elif child.name in ('functions', 'configs', 'strings', 'os_desc', 'webusb') and not list(child.iterdir()):
                child.rmdir()
        super().rmdir(path)


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fs = KernelConfigFS()
        self.commands = []
        self.modules = {'libcomposite', 'usb_f_uac2', 'dwc2'}
        self.system = gadget.GadgetSystem(self.root, run=self.run_command, fs=self.fs)
        self.put('sys/kernel/config/usb_gadget/.keep', '')
        self.put('etc/machine-id', 'test-machine')

    def put(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def udc(self, name='fe980000.usb', state='not attached', function=''):
        self.put(f'sys/class/udc/{name}/state', state)
        self.put(f'sys/class/udc/{name}/function', function)

    def run_command(self, args):
        self.commands.append(args)
        if args[0] == 'modprobe' and args[-1] not in self.modules:
            raise RuntimeError('Module not found')
        return ''

    def test_no_udc_hides_option_even_with_pi_model(self):
        self.put('sys/firmware/devicetree/base/model', 'Raspberry Pi 4 Model B\0')
        self.assertFalse(self.system.status()['supported'])
        with self.assertRaisesRegex(RuntimeError, 'controller'):
            self.system.set_enabled(True)
        self.assertFalse(self.system.status()['enabled'])

    def test_generic_board_and_kernel_support(self):
        self.udc('musb-hdrc.0')
        self.assertTrue(self.system.status()['supported'])
        self.modules.remove('usb_f_uac2')
        self.assertFalse(self.system.status()['supported'])

    def test_capture_direction_persistence_and_power_only(self):
        self.udc()
        enabled = self.system.set_enabled(True)
        self.assertTrue(enabled['enabled'])
        self.assertTrue(enabled['active'])
        self.assertFalse(enabled['connected'])
        base = self.system.gadget
        self.assertEqual((base / 'functions/uac2.usb0/c_chmask').read_text().strip(), '3')
        self.assertEqual((base / 'functions/uac2.usb0/p_chmask').read_text().strip(), '0')
        self.assertEqual((base / 'functions/uac2.usb0/c_srate').read_text().strip(), '48000')
        self.assertEqual((base / 'UDC').read_text().strip(), 'fe980000.usb')
        before = (base / 'UDC').stat().st_mtime_ns
        self.system.set_enabled(True)
        self.assertEqual((base / 'UDC').stat().st_mtime_ns, before)
        shutil.rmtree(base)
        fresh = gadget.GadgetSystem(self.root, run=self.run_command, fs=self.fs)
        self.assertTrue(fresh.restore()['active'])
        self.assertTrue(fresh.status()['enabled'])
        self.assertFalse(fresh.set_enabled(False)['active'])
        self.assertFalse(base.exists())
        self.assertFalse(fresh.restore()['enabled'])
        self.assertFalse(fresh.set_enabled(False)['active'])
        self.assertFalse(any('-r' in command for command in self.commands))

    def test_foreign_gadget_is_not_changed(self):
        self.udc(function='Other gadget')
        foreign = self.put('sys/kernel/config/usb_gadget/other/UDC', 'fe980000.usb')
        with self.assertRaisesRegex(RuntimeError, 'occupied'):
            self.system.set_enabled(True)
        self.assertEqual(foreign.read_text(), 'fe980000.usb')
        self.assertFalse(self.system.set_enabled(False)['active'])
        self.assertEqual(foreign.read_text(), 'fe980000.usb')

    def test_same_name_foreign_gadget_is_never_adopted(self):
        self.udc()
        path = self.put(f'sys/kernel/config/usb_gadget/{gadget.GADGET_NAME}/UDC', '')
        with self.assertRaisesRegex(RuntimeError, 'owned'):
            self.system.set_enabled(True)
        with self.assertRaisesRegex(RuntimeError, 'owned'):
            self.system.set_enabled(False)
        self.assertTrue(path.exists())

    def test_multiple_free_controllers_are_ambiguous(self):
        self.udc('one')
        self.udc('two')
        with self.assertRaisesRegex(RuntimeError, 'Multiple'):
            self.system.set_enabled(True)
        self.assertFalse(self.system.gadget.exists())

    def test_free_controller_does_not_disturb_occupied_one(self):
        self.udc('one', function='Other gadget')
        self.udc('two')
        foreign = self.put('sys/kernel/config/usb_gadget/other/UDC', 'one')
        self.assertEqual(self.system.set_enabled(True)['udc'], 'two')
        self.system.set_enabled(False)
        self.assertEqual(foreign.read_text(), 'one')

    def test_bind_failure_rolls_back_and_does_not_save_on(self):
        self.udc()
        self.fs.fail_bind = True
        with self.assertRaisesRegex(OSError, 'busy'):
            self.system.set_enabled(True)
        self.assertFalse(self.system.gadget.exists())
        self.assertFalse(self.system.status()['enabled'])

    def test_modified_owned_gadget_is_preserved(self):
        self.udc()
        self.system.set_enabled(True)
        foreign = self.system.gadget / 'functions/ecm.other'
        foreign.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'modified'):
            self.system.set_enabled(False)
        self.assertTrue(foreign.exists())
        self.assertTrue(self.system.status()['active'])

    def test_added_configuration_strings_are_checked_before_unbinding(self):
        self.udc()
        self.system.set_enabled(True)
        foreign = self.system.gadget / 'configs/c.1/strings/0x407'
        foreign.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'modified'):
            self.system.set_enabled(False)
        self.assertEqual((self.system.gadget / 'UDC').read_text().strip(), 'fe980000.usb')
        self.assertTrue(self.system.status()['enabled'])

    def test_busy_controller_is_not_hot_switched_to_another_after_restart(self):
        self.udc('dedicated')
        self.system.set_enabled(True)
        shutil.rmtree(self.system.gadget)
        self.put('sys/class/udc/dedicated/function', 'Other gadget')
        self.udc('other-port')
        with self.assertRaisesRegex(RuntimeError, 'saved.*controller'):
            self.system.restore()
        self.assertFalse(self.system.gadget.exists())
        self.assertTrue(self.system.status()['enabled'])

    def test_boot_failure_keeps_on_for_retry_after_controller_appears(self):
        self.udc()
        self.system.set_enabled(True)
        shutil.rmtree(self.system.gadget)
        shutil.rmtree(self.root / 'sys/class/udc')
        with self.assertRaisesRegex(RuntimeError, 'controller'):
            self.system.restore()
        self.assertTrue(self.system.status()['enabled'])
        self.udc()
        self.assertTrue(self.system.restore()['active'])

    def pi_boot(self, model='raspberrypi,4-model-b'):
        self.put('sys/firmware/devicetree/base/compatible', model + '\0brcm,bcm2711\0')
        self.put('sys/firmware/devicetree/base/__symbols__/usb', '/soc/usb@7e980000\0')
        self.put('sys/firmware/devicetree/base/soc/usb@7e980000/compatible', 'brcm,bcm2835-usb\0')
        self.put('boot/firmware/overlays/dwc2.dtbo', 'overlay')
        return self.put('boot/firmware/config.txt', '[all]\ndtparam=audio=on\n')

    def check_pi_boot(self, model):
        config = self.pi_boot(model)
        self.assertTrue(self.system.status()['supported'])
        self.assertFalse(self.system.status()['udc_available'])
        result = self.system.set_enabled(True)
        self.assertTrue(result['reboot_required'])
        self.assertTrue(result['enabled'])
        self.assertFalse(result['active'])
        self.assertIn('dtoverlay=dwc2,dr_mode=peripheral', config.read_text())
        self.assertIn('dtparam=audio=on', config.read_text())
        self.system.set_enabled(False)
        self.udc()
        self.assertFalse(self.system.set_enabled(True)['reboot_required'])
        self.assertFalse(self.system.set_enabled(False)['reboot_required'])

    def test_pi4_boot_preparation_then_hot_toggles(self):
        self.check_pi_boot('raspberrypi,4-model-b')

    def test_pi5_boot_preparation_then_hot_toggles(self):
        self.check_pi_boot('raspberrypi,5-model-b')

    def test_restore_waits_for_boot_controller_instead_of_reporting_success(self):
        config = self.pi_boot()
        self.system.set_enabled(True)
        before = config.read_text()
        with self.assertRaisesRegex(RuntimeError, 'controller.*ready'):
            self.system.restore()
        self.assertEqual(config.read_text(), before)
        self.udc()
        self.assertTrue(self.system.restore()['active'])

    def test_pi_conflicting_boot_include_is_preserved(self):
        config = self.pi_boot()
        config.write_text('[all]\ninclude usercfg.txt\n')
        include = self.put('boot/firmware/usercfg.txt', 'otg_mode=1\n')
        with self.assertRaisesRegex(RuntimeError, 'boot'):
            self.system.set_enabled(True)
        self.assertEqual(config.read_text(), '[all]\ninclude usercfg.txt\n')
        self.assertEqual(include.read_text(), 'otg_mode=1\n')

    def test_no_blind_boot_edit_without_controller_node(self):
        config = self.pi_boot()
        (self.root / 'sys/firmware/devicetree/base/__symbols__/usb').unlink()
        self.assertFalse(self.system.status()['supported'])
        self.assertEqual(config.read_text(), '[all]\ndtparam=audio=on\n')


if __name__ == '__main__':
    unittest.main()
