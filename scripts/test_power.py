#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the systemd-logind power backend (audio/power.py).

These tests do NOT touch the real dbus-send binary or the system bus:

* An in-process fake ``runner`` captures the args passed to the
  ``dbus-send`` child and returns scripted stdout/stderr/return-code
  values.
* The fake replays the exact text shapes dbus-send actually produces
  (verified locally by running dbus-send against a no-systemd host), so
  the parsing helpers see the same payload they would in production.
* Every test swaps the module-level singleton via
  ``power.set_backend`` so main.py cannot leak real state between
  tests.

The behaviour covered here:

* ``Manager.CanSuspend`` / ``Manager.CanPowerOff`` textual answers map
  to the right capability payload.
* ``Suspend`` / ``PowerOff`` results distinguish ``ok``, ``denied``
  (polkit-flavoured errors), and ``unavailable`` (login1 unreachable).
* Unknown actions are rejected before dispatching a dbus-send child.
* Suspend / power-off are passed to dbus-send with the boolean
  ``false`` (non-interactive) and the matching ``Manager`` method.
* The runner is invoked with positional args only -- never with a
  shell -- so the OAuth-style ``shell=True`` regression class is
  impossible at this layer.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest
from typing import List, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from audio import power


class _ScriptedRunner:
    """Async fake that returns preseeded subprocess results.

    Each call to ``__call__`` pops the next scripted result.  The full
    argument list is captured so tests can verify the args passed to
    dbus-send are still hard-coded (no user-supplied input, no shell).
    """

    def __init__(self, results: List[Tuple[int, str, str]]):
        self._results = list(results)
        self.calls: List[Tuple[str, ...]] = []

    async def __call__(self, *args: str, timeout: float = 0.0):
        self.calls.append(tuple(args))
        if not self._results:
            raise AssertionError("ScriptedRunner ran out of scripted results")
        returncode, stdout, stderr = self._results.pop(0)
        return power._SubprocessResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )


CAN_SUSPEND_YES = """\
method return time=1700000000.000000 serial=15 reply_serial=2
   string "yes"
"""

CAN_SUSPEND_YES_LEGACY = """\
method_return time=1700000000.000000 serial=15 reply_serial=2
   variant       string "yes"
"""

CAN_SUSPEND_NO = """\
method return time=1700000000.000000 serial=15 reply_serial=2
   string "no"
"""

CAN_POWEROFF_CHALLENGE = """\
method return time=1700000000.000000 serial=15 reply_serial=2
   string "challenge"
"""

CAN_POWEROFF_YES = """\
method return time=1700000000.000000 serial=16 reply_serial=2
   string "yes"
"""

UNKNOWN_METHOD = (
    "Error org.freedesktop.DBus.Error.UnknownMethod: "
    "Method 'CanSuspend' with interface 'org.freedesktop.login1.Manager' "
    "not found.\n"
)

SERVICE_UNKNOWN = (
    "Error org.freedesktop.DBus.Error.ServiceUnknown: "
    "The name org.freedesktop.login1 was not provided by any .service files\n"
)

PERMISSION_DENIED = (
    "Error org.freedesktop.login1.PermissionDenied: "
    "Action inhibited, try again later.\n"
)

INTERACTIVE_AUTH = (
    "Error org.freedesktop.DBus.Error.InteractiveAuthorizationRequired: "
    "Access denied as the requested operation requires interactive "
    "authentication. However, interactive authentication has not been "
    "enabled by the calling program.\n"
)


def _run(coro):
    return asyncio.run(coro)


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self._previous_backend = power._power_backend

    def tearDown(self):
        power._power_backend = self._previous_backend

    def test_both_yes(self):
        # Modern logind: capability probes are direct method calls.
        # Each probe runs once; the method reply shape (bare `string "..."`)
        # reaches the parser without a leftover `variant` prefix.
        runner = _ScriptedRunner([
            (0, CAN_SUSPEND_YES, ""),
            (0, CAN_POWEROFF_CHALLENGE, ""),
        ])
        power.set_backend(power.PowerBackend(runner=runner))

        caps = _run(power.get_capabilities())

        self.assertTrue(caps.available)
        self.assertEqual(caps.suspend, "yes")
        self.assertEqual(caps.power_off, "challenge")
        self.assertIsNone(caps.unavailable_reason)
        self.assertEqual(len(runner.calls), 2)
        names = [self._probe_method_name(args) for args in runner.calls]
        self.assertEqual(names, ["CanSuspend", "CanPowerOff"])
        for args in runner.calls:
            self.assertEqual(args[0], "dbus-send")
            self.assertIn("--system", args)
            self.assertIn("--print-reply", args)
            self.assertIn("--dest=org.freedesktop.login1", args)
            self.assertIn("/org/freedesktop/login1", args)
            # The property-form Get MUST NOT appear on the happy path:
            # the modern method form is always tried first and only
            # falls back on UnknownMethod / UnknownProperty replies.
            self.assertNotIn("org.freedesktop.DBus.Properties.Get", args)

    def test_legacy_property_form_fallback(self):
        # Older logind: the method probe errors with UnknownMethod and
        # the runner falls back to ``Properties.Get`` whose reply uses
        # the ``variant string "..."`` shape.
        runner = _ScriptedRunner([
            (1, "", UNKNOWN_METHOD),
            (0, CAN_SUSPEND_YES_LEGACY, ""),
            (1, "", UNKNOWN_METHOD),
            (0, CAN_SUSPEND_YES_LEGACY, ""),
        ])
        power.set_backend(power.PowerBackend(runner=runner))

        caps = _run(power.get_capabilities())

        self.assertTrue(caps.available)
        self.assertEqual(caps.suspend, "yes")
        self.assertEqual(caps.power_off, "yes")
        self.assertEqual(len(runner.calls), 4)
        # First two are the suspend probe (method then property);
        # the next two are the power_off probe (same fallback path).
        self.assertEqual(
            self._probe_method_name(runner.calls[0]), "CanSuspend"
        )
        self.assertIn(
            "org.freedesktop.DBus.Properties.Get", runner.calls[1]
        )
        self.assertEqual(
            self._probe_method_name(runner.calls[2]), "CanPowerOff"
        )
        self.assertIn(
            "org.freedesktop.DBus.Properties.Get", runner.calls[3]
        )

    def test_method_form_failure_with_no_legacy_fallback(self):
        # When the method probe fails for any reason that is neither
        # UnknownMethod nor UnknownProperty, the property-form fallback
        # is skipped (we cannot read the capability cleanly, so collapse
        # to "unavailable" rather than risk a stale answer).
        runner = _ScriptedRunner([
            (1, "", PERMISSION_DENIED),
            (1, "", PERMISSION_DENIED),
        ])
        power.set_backend(power.PowerBackend(runner=runner))

        caps = _run(power.get_capabilities())

        self.assertFalse(caps.available)
        self.assertEqual(caps.suspend, "unavailable")
        self.assertEqual(caps.power_off, "unavailable")
        # Only the method probes ran; the fallback path was skipped.
        self.assertEqual(len(runner.calls), 2)
        for args in runner.calls:
            self.assertNotIn(
                "org.freedesktop.DBus.Properties.Get", args
            )

    @staticmethod
    def _probe_method_name(args):
        """Return the managed method name embedded in a dbus-send arg."""

        for token in args:
            if token.startswith("org.freedesktop.login1.Manager.Can"):
                return token.rsplit(".", 1)[-1]
        return None

    def test_suspend_no_and_power_off_unknown(self):
        runner = _ScriptedRunner([
            (0, CAN_SUSPEND_NO, ""),
            (1, "", SERVICE_UNKNOWN),
        ])
        power.set_backend(power.PowerBackend(runner=runner))
        caps = _run(power.get_capabilities())
        self.assertTrue(caps.available)  # At least one capability probe ran.
        self.assertEqual(caps.suspend, "no")
        self.assertEqual(caps.power_off, "unavailable")

    def test_both_unavailable_when_logind_missing(self):
        runner = _ScriptedRunner([
            (0, SERVICE_UNKNOWN, ""),
            (0, SERVICE_UNKNOWN, ""),
        ])
        power.set_backend(power.PowerBackend(runner=runner))
        caps = _run(power.get_capabilities())
        self.assertFalse(caps.available)
        self.assertEqual(caps.suspend, "unavailable")
        self.assertEqual(caps.power_off, "unavailable")
        self.assertIsNotNone(caps.unavailable_reason)

    def test_file_missing_runner_is_treated_as_unavailable(self):
        def missing_runner(*args, **kwargs):
            raise FileNotFoundError("dbus-send")

        power.set_backend(power.PowerBackend(runner=missing_runner))
        caps = _run(power.get_capabilities())
        self.assertFalse(caps.available)
        self.assertEqual(caps.suspend, "unavailable")
        self.assertEqual(caps.power_off, "unavailable")


class ActionTests(unittest.TestCase):
    def setUp(self):
        self._previous_backend = power._power_backend

    def tearDown(self):
        power._power_backend = self._previous_backend

    def test_suspend_success(self):
        runner = _ScriptedRunner([
            (0, "method_return time=1700000000.000000 serial=17 reply_serial=2\n", ""),
        ])
        power.set_backend(power.PowerBackend(runner=runner))

        result = _run(power.request_suspend())
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.action, "suspend")
        self.assertEqual(runner.calls, [
            (
                "dbus-send",
                "--system",
                "--print-reply",
                "--dest=org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager.Suspend",
                "boolean:false",
            )
        ])

    def test_power_off_success(self):
        runner = _ScriptedRunner([
            (0, "method_return time=1700000000.000000 serial=18 reply_serial=2\n", ""),
        ])
        power.set_backend(power.PowerBackend(runner=runner))

        result = _run(power.request_power_off())
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.action, "power_off")
        self.assertEqual(
            runner.calls[0][-2:],
            ("org.freedesktop.login1.Manager.PowerOff", "boolean:false"),
        )

    def test_polkit_denied(self):
        runner = _ScriptedRunner([(1, "", PERMISSION_DENIED)])
        power.set_backend(power.PowerBackend(runner=runner))
        result = _run(power.request_suspend())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "denied")
        self.assertIn("inhibited", result.error or "")

    def test_interactive_auth_is_denied_status(self):
        runner = _ScriptedRunner([(1, "", INTERACTIVE_AUTH)])
        power.set_backend(power.PowerBackend(runner=runner))
        result = _run(power.request_power_off())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "denied")

    def test_service_unknown_is_unavailable(self):
        runner = _ScriptedRunner([(1, "", SERVICE_UNKNOWN)])
        power.set_backend(power.PowerBackend(runner=runner))
        result = _run(power.request_suspend())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")

    def test_unknown_action_rejected_without_runner(self):
        runner = _ScriptedRunner([])
        power.set_backend(power.PowerBackend(runner=runner))
        result = _run(power.PowerBackend(runner=runner)._invoke_logind_action("reboot"))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "denied")
        self.assertEqual(runner.calls, [])

    def test_is_now_supported_rejects_unknown_action_without_probe(self):
        runner = _ScriptedRunner([])
        power.set_backend(power.PowerBackend(runner=runner))
        supported, raw = _run(power.is_now_supported("reboot"))
        self.assertFalse(supported)
        self.assertEqual(raw, "invalid-action")
        # An unknown action must not alias an existing probe (e.g. power_off).
        self.assertEqual(runner.calls, [])

    def test_is_now_supported_reports_matching_capability(self):
        runner = _ScriptedRunner([
            (0, CAN_SUSPEND_YES, ""),
            (0, CAN_POWEROFF_YES, ""),
        ])
        power.set_backend(power.PowerBackend(runner=runner))
        supported, raw = _run(power.is_now_supported("suspend"))
        self.assertTrue(supported)
        self.assertEqual(raw, "yes")
        # Both capabilities are probed by get_capabilities; only the
        # suspend answer decides the result.
        self.assertEqual(len(runner.calls), 2)

    def test_dbus_send_missing_runner_yields_unavailable(self):
        def missing_runner(*args, **kwargs):
            raise FileNotFoundError("dbus-send")

        power.set_backend(power.PowerBackend(runner=missing_runner))
        result = _run(power.request_power_off())
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("dbus-send", result.error or "")


class LogindVocabularySupportTests(unittest.TestCase):
    """The power module reports the raw logind answer verbatim and
    never attempts to coerce ``challenge`` / inhibitor variants into
    supportable capability values.  These tests pin the contract
    that downstream code (main.py, the JS frontend, the polkit rule)
    relies on: the textual field is the source of truth."""

    def setUp(self):
        self._previous_backend = power._power_backend

    def tearDown(self):
        power._power_backend = self._previous_backend

    def test_full_logind_vocabulary_parses_with_value_verbatim(self):
        for value in [
            "yes",
            "no",
            "na",
            "challenge",
            "inhibited",
            "inhibitor-blocked",
            "challenge-inhibitor-blocked",
        ]:
            payload = (
                "method_return time=1700000000.000000 serial=1 reply_serial=2\n"
                f'   variant       string "{value}"\n'
            )
            runner = _ScriptedRunner([(0, payload, ""), (0, payload, "")])
            power.set_backend(power.PowerBackend(runner=runner))

            caps = _run(power.get_capabilities())

            self.assertTrue(caps.available, msg=f"value={value!r}")
            # suspend and power_off get the exact same mocked text.
            self.assertEqual(caps.suspend, value, msg=f"value={value!r}")
            self.assertEqual(caps.power_off, value, msg=f"value={value!r}")

    def test_no_logind_value_implies_availabilty_false(self):
        # logind unreachable -> both probes fail.  The module must NOT
        # claim the action is supported; the UI must hide the menu
        # entries and a POST must be rejected before dispatching.
        runner = _ScriptedRunner(
            [(0, "", SERVICE_UNKNOWN), (0, "", SERVICE_UNKNOWN)]
        )
        power.set_backend(power.PowerBackend(runner=runner))
        caps = _run(power.get_capabilities())
        self.assertFalse(caps.available)
        self.assertEqual(caps.suspend, "unavailable")
        self.assertEqual(caps.power_off, "unavailable")


class ParsingTests(unittest.TestCase):
    def test_variant_string_yes(self):
        self.assertEqual(power._parse_dbus_string_variant(CAN_SUSPEND_YES), "yes")

    def test_variant_string_no(self):
        self.assertEqual(power._parse_dbus_string_variant(CAN_SUSPEND_NO), "no")

    def test_variant_string_challenge(self):
        self.assertEqual(
            power._parse_dbus_string_variant(CAN_POWEROFF_CHALLENGE), "challenge"
        )

    def test_variant_string_missing(self):
        self.assertIsNone(power._parse_dbus_string_variant(""))

    def test_error_full_shape(self):
        name, msg = power._parse_dbus_error(PERMISSION_DENIED)
        self.assertEqual(name, "org.freedesktop.login1.PermissionDenied")
        self.assertIn("inhibited", msg)

    def test_error_bare_shape(self):
        name, msg = power._parse_dbus_error(
            "org.freedesktop.DBus.Error.AccessDenied: nope\n"
        )
        self.assertEqual(name, "org.freedesktop.DBus.Error.AccessDenied")
        self.assertEqual(msg, "nope")


if __name__ == "__main__":
    unittest.main()
