# SPDX-License-Identifier: AGPL-3.0-only
"""Unprivileged USB gadget status and existing ALSA input integration."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from audio.usb_gadget_system import COMMAND_ENV, HELPER_PATH, GadgetSystem, read_text

SYSTEM = GadgetSystem()


def helper_installed() -> bool:
    path = Path(HELPER_PATH)
    try:
        info = path.stat()
        return path.is_file() and not path.is_symlink() and info.st_uid == 0 and not info.st_mode & 0o022
    except OSError:
        return False


def status() -> dict:
    result = SYSTEM.status()
    installed = helper_installed()
    result['helper_installed'] = installed
    result['can_enable'] = result['supported'] and installed and not result['conflict']
    if result['supported'] and not installed:
        result['message'] = 'System setup is required. Run the FXRoute USB gadget installer, then reopen this dialog.'
    return result


def set_enabled(enabled: bool) -> dict:
    if type(enabled) is not bool:
        raise ValueError('enabled must be a boolean')
    if not helper_installed():
        raise RuntimeError('USB gadget system helper is not installed. Run scripts/install_usb_gadget.sh as root with the FXRoute user name.')
    result = subprocess.run(
        ['sudo', '-n', HELPER_PATH, 'on' if enabled else 'off'],
        capture_output=True, text=True, timeout=60, env=COMMAND_ENV,
    )
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        payload = {}
    if result.returncode or not isinstance(payload, dict) or 'error' in payload:
        error = payload.get('error') if isinstance(payload, dict) else None
        raise RuntimeError(error or result.stderr.strip() or 'USB gadget configuration failed.')
    return status()


def capture_connection(card: int | None) -> bool | None:
    """None for ordinary cards; otherwise a fresh, fail-closed UDC connection."""
    if type(card) is not int or card < 0:
        return None
    sound = SYSTEM.root / f'sys/class/sound/card{card}'
    card_id = read_text(sound / 'id').lower()
    device = (sound / 'device').resolve()
    gadget_card = 'gadget' in card_id or 'gadget' in device.parts
    if not gadget_card:
        return None
    for udc in (SYSTEM.root / 'sys/class/udc').glob('*'):
        controller = (udc / 'device').resolve() if (udc / 'device').exists() else udc.resolve().parent.parent
        if device == controller or controller in device.parents:
            return read_text(udc / 'state') == 'configured'
    return False
