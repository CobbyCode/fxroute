#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only

"""Minimal BlueZ audio agent for headless/no-input Bluetooth audio pairing."""

from __future__ import annotations

import signal
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BUS_NAME = "org.bluez"
AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
AGENT_PATH = "/fxroute/agent"
CAPABILITY = "DisplayYesNo"

AUDIO_UUID_PREFIXES = {
    "0000110a",  # Audio Source
    "0000110b",  # Audio Sink
    "0000110c",  # AVRCP Target
    "0000110e",  # AVRCP Controller
    "0000111e",  # Handsfree
    "0000111f",  # Handsfree Audio Gateway
}


class Rejected(dbus.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class Agent(dbus.service.Object):
    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Release(self):
        mainloop.quit()

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        return "0000"

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_INTERFACE, in_signature="ou", out_signature="")
    def DisplayPasskey(self, device, passkey):
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        normalized = str(uuid).strip().lower()
        if any(normalized.startswith(prefix) for prefix in AUDIO_UUID_PREFIXES):
            return
        # Be permissive for already-paired device services while agent is active.
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Cancel(self):
        return


mainloop: GLib.MainLoop


def _quit(*_args):
    try:
        mainloop.quit()
    except Exception:
        pass


def main() -> int:
    global mainloop
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    agent = Agent(bus, AGENT_PATH)
    manager = dbus.Interface(bus.get_object(BUS_NAME, "/org/bluez"), AGENT_MANAGER_INTERFACE)
    manager.RegisterAgent(AGENT_PATH, CAPABILITY)
    manager.RequestDefaultAgent(AGENT_PATH)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _quit)

    mainloop = GLib.MainLoop()
    try:
        mainloop.run()
    finally:
        try:
            manager.UnregisterAgent(AGENT_PATH)
        except Exception:
            pass
        del agent
    return 0


if __name__ == "__main__":
    sys.exit(main())
