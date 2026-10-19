#!/usr/bin/python3 -I
# SPDX-License-Identifier: AGPL-3.0-only
"""Standalone, root-owned UAC2 controller. No application imports or CLI paths."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

GADGET_NAME = 'fxroute_audio'
HELPER_PATH = '/usr/local/sbin/fxroute-usb-gadget'
BOOT_BLOCK = '\n# FXRoute USB audio controller\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n'
COMMAND_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}


def run_command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=15, env=COMMAND_ENV)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f'{args[0]} failed')
    return result.stdout


def read_text(path: Path) -> str:
    try:
        return path.read_text().strip().rstrip('\0')
    except OSError:
        return ''


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class ConfigFS:
    """ConfigFS attributes are kernel files; remove directories, never attributes."""

    def mkdir(self, path: Path) -> None:
        path.mkdir()

    def write(self, path: Path, value: str) -> None:
        path.write_text(value + '\n')

    def rmdir(self, path: Path) -> None:
        path.rmdir()


class GadgetSystem:
    def __init__(self, root: Path = Path('/'), *, run=run_command, fs=None):
        self.root = root
        self.run = run
        self.fs = fs or ConfigFS()
        self.configfs = root / 'sys/kernel/config/usb_gadget'
        self.gadget = self.configfs / GADGET_NAME
        self.state_dir = root / 'var/lib/fxroute-usb-gadget'
        self.state_path = self.state_dir / 'state.json'
        self.owner_path = self.state_dir / 'owner'

    def _state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text())
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, enabled: bool, udc: str | None = None) -> None:
        atomic_write(self.state_path, json.dumps({'enabled': enabled, 'udc': udc}) + '\n')

    def _boot_id(self) -> str:
        return read_text(self.root / 'proc/sys/kernel/random/boot_id') or 'unknown'

    def _serial(self) -> str:
        machine = read_text(self.root / 'etc/machine-id') or 'fxroute'
        return 'fxroute-' + hashlib.sha256(machine.encode()).hexdigest()[:16]

    def _module_available(self, name: str) -> bool:
        if (self.root / 'sys/module' / name).is_dir():
            return True
        try:
            self.run(['modprobe', '-n', '-q', name])
            return True
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return False

    def _controllers(self) -> list[dict]:
        return [
            {'name': path.name, 'state': read_text(path / 'state'),
             'function': read_text(path / 'function')}
            for path in sorted((self.root / 'sys/class/udc').glob('*')) if path.is_dir()
        ]

    def _boot_config(self) -> Path | None:
        """Only Pi 4/5's dedicated controller with a known firmware overlay."""
        dt = self.root / 'sys/firmware/devicetree/base'
        compatible = read_text(dt / 'compatible').split('\0')
        if not {'raspberrypi,4-model-b', 'raspberrypi,5-model-b'}.intersection(compatible):
            return None
        target = read_text(dt / '__symbols__/usb')
        if not target.startswith('/') or '..' in Path(target).parts:
            return None
        controller = read_text(dt / target.lstrip('/') / 'compatible').split('\0')
        if not {'brcm,bcm2835-usb', 'snps,dwc2'}.intersection(controller):
            return None
        if not self._module_available('dwc2'):
            return None
        for directory in ('boot/firmware', 'boot'):
            base = self.root / directory
            if (base / 'config.txt').is_file() and (base / 'overlays/dwc2.dtbo').is_file():
                return base / 'config.txt'
        return None

    def _boot_conflict(self, config: Path, seen=None) -> str | None:
        seen = set() if seen is None else seen
        resolved = config.resolve()
        if resolved in seen or len(seen) >= 16:
            return 'Cannot safely inspect recursive boot includes.'
        seen.add(resolved)
        try:
            text = config.read_text().replace(BOOT_BLOCK, '')
        except OSError:
            return 'Cannot read the boot configuration.'
        for raw in text.splitlines():
            line = raw.split('#', 1)[0].strip()
            if re.match(r'(dtoverlay\s*=\s*(dwc2|dwc-otg)(,|$)|otg_mode\s*=\s*1)', line):
                return 'Existing USB boot configuration needs manual review before enabling device mode.'
            if line.startswith('include '):
                include = line[8:].strip()
                if Path(include).is_absolute() or '..' in Path(include).parts:
                    return 'Cannot safely inspect the boot include.'
                conflict = self._boot_conflict(config.parent / include, seen)
                if conflict:
                    return conflict
        return None

    def _owned(self) -> bool:
        return read_text(self.owner_path) == self._boot_id()

    def _check_owned(self) -> None:
        if not self._owned():
            raise RuntimeError('Existing gadget is not owned by FXRoute.')
        serial = read_text(self.gadget / 'strings/0x409/serialnumber')
        functions = {path.name for path in (self.gadget / 'functions').iterdir()}
        configs = {path.name for path in (self.gadget / 'configs').iterdir()}
        links = [path for path in self.gadget.rglob('*') if path.is_symlink()]
        expected_link = self.gadget / 'configs/c.1/uac2.usb0'
        allowed_directories = {
            'functions', 'functions/uac2.usb0', 'configs', 'configs/c.1',
            'configs/c.1/strings', 'configs/c.1/strings/0x409',
            'strings', 'strings/0x409', 'os_desc', 'webusb',
        }
        if any(str(path.relative_to(self.gadget)) not in allowed_directories
               for path in self.gadget.rglob('*') if path.is_dir() and not path.is_symlink()):
            raise RuntimeError('The FXRoute gadget directories were modified; leaving them untouched.')
        if (serial and serial != self._serial()) or functions - {'uac2.usb0'} or configs - {'c.1'}:
            raise RuntimeError('The FXRoute gadget was modified; leaving it untouched.')
        if any(path != expected_link or path.resolve() != self.gadget / 'functions/uac2.usb0' for path in links):
            raise RuntimeError('The FXRoute gadget links were modified; leaving them untouched.')

    def status(self) -> dict:
        controllers = self._controllers()
        kernel = all(self._module_available(name) for name in ('libcomposite', 'usb_f_uac2'))
        boot_config = self._boot_config() if not controllers else None
        state = self._state()
        bound = read_text(self.gadget / 'UDC') if self._owned() else ''
        controller = next((entry for entry in controllers if entry['name'] == bound), {})
        conflict = None
        if self.gadget.exists():
            try:
                self._check_owned()
            except (RuntimeError, OSError) as exc:
                conflict = str(exc)
        elif boot_config:
            conflict = self._boot_conflict(boot_config)
        occupied = {read_text(path / 'UDC') for path in self.configfs.glob('*') if path.is_dir() and path != self.gadget}
        free = [entry['name'] for entry in controllers if not entry['function'] and entry['name'] not in occupied]
        if not bound and controllers and not conflict:
            if state.get('udc') and state['udc'] not in free:
                conflict = 'The saved USB device controller is missing or occupied; leaving other ports untouched.'
            elif not free:
                conflict = 'All USB device controllers are occupied by another gadget.'
            elif len(free) > 1 and state.get('udc') not in free:
                conflict = 'Multiple free USB device controllers; select one through system configuration first.'
        supported = kernel and bool(controllers or boot_config)
        reboot = bool(state.get('enabled') and not controllers and boot_config)
        if not kernel:
            message = 'Kernel UAC2/ConfigFS support is unavailable.'
        elif not controllers and not boot_config:
            message = 'No USB device controller is available. Configure a dedicated peripheral port in the board boot configuration first.'
        elif conflict:
            message = conflict
        elif reboot:
            message = 'Restart required to enable the USB device controller.'
        elif not controllers:
            message = 'First activation prepares the dedicated USB-C controller and requires a restart.'
        elif bound:
            message = 'USB host connected.' if controller.get('state') == 'configured' else 'Enabled; waiting for a USB host. Power-only operation is available.'
        else:
            message = 'USB Audio Gadget is off.'
        return {
            'supported': supported, 'kernel_supported': kernel,
            'udc_available': bool(controllers), 'controllers': controllers,
            'enabled': state.get('enabled') is True, 'active': bool(bound and controller),
            'udc': bound or None, 'connected': controller.get('state') == 'configured',
            'connection_state': controller.get('state') or 'unavailable',
            'reboot_required': reboot, 'setup_requires_reboot': bool(boot_config),
            'conflict': conflict, 'message': message,
        }

    def _prepare_configfs(self) -> None:
        self.run(['modprobe', 'libcomposite'])
        self.run(['modprobe', 'usb_f_uac2'])
        if not self.configfs.is_dir():
            self.run(['mount', '-t', 'configfs', 'configfs', str(self.configfs.parent)])
        if not self.configfs.is_dir():
            raise RuntimeError('ConfigFS USB gadget support is unavailable.')

    def _remove(self) -> None:
        if not self.gadget.exists():
            return
        self._check_owned()
        if read_text(self.gadget / 'UDC'):
            self.fs.write(self.gadget / 'UDC', '')
        link = self.gadget / 'configs/c.1/uac2.usb0'
        if link.is_symlink():
            link.unlink()
        for relative in ('configs/c.1/strings/0x409', 'configs/c.1',
                         'functions/uac2.usb0', 'strings/0x409'):
            path = self.gadget / relative
            if path.exists():
                self.fs.rmdir(path)
        self.fs.rmdir(self.gadget)
        self.owner_path.unlink(missing_ok=True)

    def _create(self, udc: str) -> None:
        self.fs.mkdir(self.gadget)
        atomic_write(self.owner_path, self._boot_id())
        try:
            for relative, value in {
                'idVendor': '0x1d6b', 'idProduct': '0x0104',
                'bcdUSB': '0x0200', 'bcdDevice': '0x0100',
            }.items():
                self.fs.write(self.gadget / relative, value)
            self.fs.mkdir(self.gadget / 'strings/0x409')
            for key, value in {'manufacturer': 'FXRoute', 'product': 'FXRoute USB Audio',
                               'serialnumber': self._serial()}.items():
                self.fs.write(self.gadget / 'strings/0x409' / key, value)
            self.fs.mkdir(self.gadget / 'functions/uac2.usb0')
            # c_* is capture on the gadget: USB host playback -> ALSA capture.
            for key, value in {'c_chmask': '3', 'p_chmask': '0', 'c_srate': '48000', 'c_ssize': '4'}.items():
                self.fs.write(self.gadget / 'functions/uac2.usb0' / key, value)
            self.fs.mkdir(self.gadget / 'configs/c.1')
            self.fs.write(self.gadget / 'configs/c.1/MaxPower', '250')
            self.fs.write(self.gadget / 'configs/c.1/bmAttributes', '0x80')
            self.fs.mkdir(self.gadget / 'configs/c.1/strings/0x409')
            self.fs.write(self.gadget / 'configs/c.1/strings/0x409/configuration', 'USB audio input')
            (self.gadget / 'configs/c.1/uac2.usb0').symlink_to(self.gadget / 'functions/uac2.usb0')
            self.fs.write(self.gadget / 'UDC', udc)
        except BaseException:
            self._remove()
            raise

    def _apply(self, enabled: bool, *, persist: bool) -> dict:
        if not enabled:
            self._remove()
            if persist:
                self._save(False)
            return self.status()
        status = self.status()
        if status['conflict']:
            raise RuntimeError(status['conflict'])
        if not status['supported']:
            raise RuntimeError(status['message'])
        if status['active']:
            if persist:
                self._save(True, status['udc'])
            return self.status()
        if not status['udc_available']:
            if not persist:
                raise RuntimeError('USB device controller is not ready; retry boot restoration.')
            config = self._boot_config()
            text = config.read_text()
            if BOOT_BLOCK not in text:
                backup = config.with_name(config.name + '.fxroute-usb-backup')
                if backup.exists():
                    raise RuntimeError('Existing USB boot backup requires manual review.')
                atomic_write(backup, text)
                atomic_write(config, text + BOOT_BLOCK)
            if persist:
                self._save(True)
            return self.status()
        self._prepare_configfs()
        # Recheck after module loading/mounting exposes previously hidden gadgets.
        status = self.status()
        if status['conflict']:
            raise RuntimeError(status['conflict'])
        occupied = {read_text(path / 'UDC') for path in self.configfs.glob('*') if path.is_dir() and path != self.gadget}
        free = [entry['name'] for entry in status['controllers'] if not entry['function'] and entry['name'] not in occupied]
        saved = self._state().get('udc')
        udc = saved if saved in free else free[0]
        if self.gadget.exists():
            self._remove()
        self._create(udc)
        try:
            if persist:
                self._save(True, udc)
        except OSError:
            self._remove()
            raise
        return self.status()

    def _locked_apply(self, enabled: bool, *, persist: bool) -> dict:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
        with (self.state_dir / 'lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._apply(enabled, persist=persist)

    def set_enabled(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        return self._locked_apply(enabled, persist=True)

    def restore(self) -> dict:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
        with (self.state_dir / 'lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._apply(self._state().get('enabled') is True, persist=False)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {'status', 'on', 'off', 'restore'}:
        print(json.dumps({'error': 'Expected status, on, off or restore'}))
        return 2
    action = sys.argv[1]
    if action != 'status' and os.geteuid() != 0:
        print(json.dumps({'error': 'Root privileges are required'}))
        return 1
    try:
        system = GadgetSystem()
        result = system.status() if action == 'status' else (
            system.restore() if action == 'restore' else system.set_enabled(action == 'on'))
        print(json.dumps(result))
        return 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
