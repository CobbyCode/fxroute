#!/usr/bin/env python3
"""BlueZ agent re-registers after bluetoothd restarts.

Registrations die with the old daemon, and FXRoute never replaces a live
agent process, so without re-registration a restarted bluetoothd would have
no agent ("Authentication attempt without agent"). The agent module needs
the system dbus/gi bindings; fakes stand in for them here.
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class _DBusException(Exception):
    pass


class _Manager:
    def __init__(self, bus):
        self._bus = bus

    def RegisterAgent(self, path, capability):
        if self._bus.fail_next:
            self._bus.fail_next -= 1
            raise _DBusException("org.freedesktop.DBus.Error.UnknownObject")
        self._bus.calls.append(("RegisterAgent", path, capability))

    def RequestDefaultAgent(self, path):
        self._bus.calls.append(("RequestDefaultAgent", path))


class _Bus:
    def __init__(self):
        self.calls = []
        self.fail_next = 0

    def get_object(self, name, path):
        return (name, path)


def _load_agent_module():
    dbus = types.ModuleType("dbus")
    dbus.DBusException = _DBusException
    dbus.UInt32 = int
    dbus.Interface = lambda _proxy, _interface: _Manager(_Bus.current)
    service = types.ModuleType("dbus.service")
    service.Object = object
    service.method = lambda *_args, **_kwargs: (lambda func: func)
    mainloop = types.ModuleType("dbus.mainloop")
    glib_loop = types.ModuleType("dbus.mainloop.glib")
    dbus.service, dbus.mainloop, mainloop.glib = service, mainloop, glib_loop
    gi = types.ModuleType("gi")
    repository = types.ModuleType("gi.repository")
    repository.GLib = types.SimpleNamespace(timeout_add=None, MainLoop=object)
    gi.repository = repository
    fakes = {
        "dbus": dbus, "dbus.service": service, "dbus.mainloop": mainloop,
        "dbus.mainloop.glib": glib_loop, "gi": gi, "gi.repository": repository,
    }
    with patch.dict(sys.modules, fakes):
        spec = importlib.util.spec_from_file_location("bluez_agent_under_test", ROOT / "audio" / "bluez_agent.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


agent = _load_agent_module()


class ReregistrationTests(unittest.TestCase):
    def setUp(self):
        self.bus = _Bus()
        _Bus.current = self.bus
        self.scheduled = []
        self.registration = agent.Registration(
            self.bus, schedule=lambda delay, callback: self.scheduled.append((delay, callback)))

    def _registered(self):
        return [call for call in self.bus.calls if call[0] == "RegisterAgent"]

    def test_restarted_bluetoothd_gets_the_agent_again(self):
        self.registration.register()
        self.registration.on_owner_changed("org.bluez", ":1.10", ":1.42")
        self.assertEqual(len(self._registered()), 2)
        self.assertEqual(self.bus.calls[-1], ("RequestDefaultAgent", agent.AGENT_PATH))
        self.assertEqual(self.scheduled, [])

    def test_vanished_or_foreign_owner_is_ignored(self):
        self.registration.on_owner_changed("org.bluez", ":1.10", "")
        self.registration.on_owner_changed("org.example", "", ":1.7")
        self.assertEqual(self.bus.calls, [])

    def test_retries_until_the_agent_manager_is_exported(self):
        self.bus.fail_next = 2
        self.registration.on_owner_changed("org.bluez", "", ":1.42")
        self.assertEqual(len(self.scheduled), 1)
        delay, retry = self.scheduled[0]
        self.assertEqual(delay, agent.REREGISTER_RETRY_MS)
        self.assertTrue(retry(), "keep the GLib timer while registration still fails")
        self.assertFalse(retry(), "stop the timer once registered")
        self.assertEqual(len(self._registered()), 1)

    def test_retries_are_bounded(self):
        self.bus.fail_next = agent.REREGISTER_ATTEMPTS + 5
        self.registration.on_owner_changed("org.bluez", "", ":1.42")
        _delay, retry = self.scheduled[0]
        results = [retry() for _ in range(agent.REREGISTER_ATTEMPTS - 1)]
        self.assertFalse(results[-1], "the timer stops after the last attempt")
        self.assertEqual(self._registered(), [])


if __name__ == "__main__":
    unittest.main()
