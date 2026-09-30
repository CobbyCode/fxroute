#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only

"""Minimal BlueZ audio agent for headless/no-input Bluetooth audio pairing."""

from __future__ import annotations

import itertools
import signal
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BUS_NAME = "org.bluez"
AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
AGENT_PATH = "/fxroute/agent"
CAPABILITY = "DisplayYesNo"
REREGISTER_RETRY_MS = 500
REREGISTER_ATTEMPTS = 10

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


class Registration:
    """Keep the agent registered with the running bluetoothd instance."""

    def __init__(self, bus, schedule=None):
        self._bus = bus
        self._schedule = schedule or GLib.timeout_add
        self.manager = None

    def register(self) -> None:
        manager = dbus.Interface(self._bus.get_object(BUS_NAME, "/org/bluez"), AGENT_MANAGER_INTERFACE)
        manager.RegisterAgent(AGENT_PATH, CAPABILITY)
        manager.RequestDefaultAgent(AGENT_PATH)
        self.manager = manager

    def on_owner_changed(self, name, _old_owner, new_owner) -> None:
        """Re-register once a restarted bluetoothd owns org.bluez again.

        Registrations die with the old daemon. Without this the process
        lives on unregistered, and FXRoute never replaces a live agent, so
        incoming connections would fail ("Authentication attempt without
        agent"). Retries cover AgentManager1 not being exported yet.
        """
        if name != BUS_NAME or not new_owner:
            return
        # Counted per owner change, so an older retry timer keeps its own budget.
        attempts = itertools.count(1)

        def attempt() -> bool:
            """One registration attempt; True keeps the GLib retry timer."""
            try:
                self.register()
                return False
            except dbus.DBusException:
                return next(attempts) < REREGISTER_ATTEMPTS

        if attempt():
            self._schedule(REREGISTER_RETRY_MS, attempt)


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
    registration = Registration(bus)
    registration.register()
    bus.add_signal_receiver(
        registration.on_owner_changed,
        signal_name="NameOwnerChanged",
        dbus_interface="org.freedesktop.DBus",
        bus_name="org.freedesktop.DBus",
        arg0=BUS_NAME,
    )

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _quit)

    mainloop = GLib.MainLoop()
    try:
        mainloop.run()
    finally:
        if registration.manager is not None:
            try:
                # Fails harmlessly when bluetoothd went away meanwhile.
                registration.manager.UnregisterAgent(AGENT_PATH)
            except Exception:
                pass
        del agent
    return 0


if __name__ == "__main__":
    sys.exit(main())
