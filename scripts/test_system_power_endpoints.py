#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP-level tests for the /api/system/power routes.

These tests import the real ``main.app`` so the route registration,
request parsing and HTTP error mapping are exercised end-to-end through
the FastAPI stack.  The actual dbus-send subprocess is replaced through
``power.set_backend`` with a fake runner so the suite stays offline and
fast.

Coverage:

* ``GET /api/system/power`` returns the full capability payload
  (suspend + power_off textual answers, supported booleans, and the
  unavailable_reason when logind is missing).
* ``POST /api/system/power/suspend`` and ``/api/system/power/power-off``
  return ``200`` on a successful dbus-send child and the exact JSON
  shape (action / status) the frontend relies on.
* Polkit PermissionDenied / InteractiveAuthorizationRequired map to
  HTTP 403.
* A genuine logind-unreachable error maps to HTTP 503.
* A bounded timeout from the asyncio wrapper turns into HTTP 503.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import power
import main as main_module
from fastapi.testclient import TestClient


CAN_SUSPEND_YES = """\
method return time=1700000000.000000 serial=15 reply_serial=2
   string "yes"
"""

CAN_SUSPEND_NO = """\
method return time=1700000000.000000 serial=15 reply_serial=2
   string "no"
"""

CAN_POWEROFF_YES = """\
method return time=1700000000.000000 serial=16 reply_serial=2
   string "yes"
"""

CAN_POWEROFF_CHALLENGE = """\
method return time=1700000000.000000 serial=16 reply_serial=2
   string "challenge"
"""


def _can_reply(value: str) -> str:
    """Build a fake dbus-send method reply for a given logind CanX value.

    Modern logind's ``Manager.Can{X}`` is a method that returns a bare
    ``string "<value>"`` on stdout.  The capability parser accepts both
    this shape and the older ``Properties.Get`` variant-form reply.
    """

    return (
        'method return time=1700000000.000000 serial=17 reply_serial=2\n'
        f'   string "{value}"\n'
    )


def _can_reply_legacy(value: str) -> str:
    """Build the legacy ``Properties.Get`` reply shape for fallback tests."""

    return (
        'method_return time=1700000000.000000 serial=17 reply_serial=2\n'
        f'   variant       string "{value}"\n'
    )


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


class _ScriptedRunner:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    async def __call__(self, *args, timeout=0.0):
        self.calls.append(tuple(args))
        returncode, stdout, stderr = self._results.pop(0)
        return power._SubprocessResult(returncode, stdout, stderr)


class RoutingTests(unittest.TestCase):
    """Verify that the new endpoints are registered on the FastAPI app."""

    @classmethod
    def setUpClass(cls):
        cls.path_to_methods = {}
        for route in main_module.app.routes:
            methods = getattr(route, "methods", None) or set()
            for method in methods:
                cls.path_to_methods.setdefault(method.upper(), []).append(route.path)

    def test_capabilities_route_get(self):
        self.assertIn("/api/system/power", self.path_to_methods.get("GET", []))

    def test_suspend_route_post(self):
        self.assertIn("/api/system/power/suspend", self.path_to_methods.get("POST", []))

    def test_power_off_route_post(self):
        self.assertIn("/api/system/power/power-off", self.path_to_methods.get("POST", []))


class _PowerTestCase(unittest.TestCase):
    """Common setup: TestClient + permanently restored power backend."""

    def setUp(self):
        # Pre-fill the runner with a generous queue of "yes" capability
        # results so tests that only care about the action reply (or
        # about cross-site rejection) do not have to spell out the
        # probe traffic every time.  Individual tests override the queue
        # when they need a non-yes capability answer.
        self._runner = _ScriptedRunner(
            [(0, CAN_SUSPEND_YES, ""), (0, CAN_POWEROFF_YES, "")] * 32
        )
        self._previous = power.set_backend(power.PowerBackend(runner=self._runner))
        self._client = TestClient(main_module.app)

    def tearDown(self):
        power.set_backend(self._previous)


class CapabilityEndpointTests(_PowerTestCase):
    def test_both_supported(self):
        self._runner._results[:] = [(0, CAN_SUSPEND_YES, ""), (0, CAN_POWEROFF_YES, "")]
        resp = self._client.get("/api/system/power")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["suspend"], "yes")
        self.assertEqual(body["power_off"], "yes")
        self.assertTrue(body["suspend_supported"])
        self.assertTrue(body["power_off_supported"])
        self.assertIsNone(body["unavailable_reason"])

    def test_suspend_hidden_when_logind_says_no(self):
        self._runner._results[:] = [(0, CAN_SUSPEND_NO, ""), (0, CAN_POWEROFF_YES, "")]
        resp = self._client.get("/api/system/power")
        body = resp.json()
        self.assertEqual(body["suspend"], "no")
        self.assertFalse(body["suspend_supported"])
        self.assertEqual(body["power_off"], "yes")
        self.assertTrue(body["power_off_supported"])

    def test_challenge_is_not_executable_for_either_action(self):
        # "challenge" means logind would ask the FXRoute user for
        # interactive polkit auth.  FXRoute must NOT advertise the
        # action as supported; the menu stays hidden and a direct
        # POST must fail cleanly with HTTP 409.
        self._runner._results[:] = [
            (0, _can_reply("challenge"), ""),
            (0, _can_reply("challenge"), ""),
        ]
        resp = self._client.get("/api/system/power")
        body = resp.json()
        self.assertEqual(body["suspend"], "challenge")
        self.assertEqual(body["power_off"], "challenge")
        self.assertFalse(body["suspend_supported"])
        self.assertFalse(body["power_off_supported"])

    def test_only_yes_text_marks_actions_executable(self):
        # Walk the complete logind vocabulary -- only the exact text
        # "yes" must produce supported=True.  Every other value,
        # including the inhibitor variants, must report supported=False.
        for value in [
            "yes",
            "no",
            "na",
            "challenge",
            "inhibited",
            "inhibitor-blocked",
            "challenge-inhibitor-blocked",
        ]:
            self._runner._results[:] = [
                (0, _can_reply(value), ""),
                (0, _can_reply(value), ""),
            ]
            resp = self._client.get("/api/system/power")
            body = resp.json()
            self.assertEqual(body["suspend"], value)
            self.assertEqual(body["power_off"], value)
            self.assertEqual(
                body["suspend_supported"], value == "yes",
                msg=f"suspend_supported must be (value == 'yes') for value={value!r}",
            )
            self.assertEqual(
                body["power_off_supported"], value == "yes",
                msg=f"power_off_supported must be (value == 'yes') for value={value!r}",
            )

    def test_unavailable_when_logind_missing(self):
        self._runner._results[:] = [(1, "", SERVICE_UNKNOWN), (1, "", SERVICE_UNKNOWN)]
        resp = self._client.get("/api/system/power")
        body = resp.json()
        self.assertFalse(body["available"])
        self.assertEqual(body["suspend"], "unavailable")
        self.assertEqual(body["power_off"], "unavailable")
        self.assertFalse(body["suspend_supported"])
        self.assertFalse(body["power_off_supported"])
        self.assertIsNotNone(body["unavailable_reason"])


class ActionEndpointTests(_PowerTestCase):
    OK_REPLY = "method_return time=1700000000.000000 serial=42 reply_serial=2\n"

    def _both_yes(self):
        # First dbus-send call: CanSuspend=yes; second: CanPowerOff=yes.
        return [(0, CAN_SUSPEND_YES, ""), (0, CAN_POWEROFF_YES, "")]

    def test_suspend_success(self):
        # Gate sees yes on both probes (setUp queue) and dispatches the
        # Manager.Suspend call.  Append the action reply at the end.
        self._runner._results.append((0, self.OK_REPLY, ""))
        resp = self._client.post("/api/system/power/suspend")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "suspended")
        self.assertEqual(body["action"], "suspend")
        # The trailing call (after the two probe calls) is the action.
        self.assertEqual(
            self._runner.calls[-1][-2:],
            ("org.freedesktop.login1.Manager.Suspend", "boolean:false"),
        )

    def test_power_off_success(self):
        # Gate sees yes on both probes (setUp queue) and dispatches the
        # Manager.PowerOff call.  Append the action reply at the end.
        self._runner._results.append((0, self.OK_REPLY, ""))
        resp = self._client.post("/api/system/power/power-off")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "shutting_down")
        self.assertEqual(body["action"], "power_off")

    def test_suspend_permission_denied_is_403(self):
        # Override the queue: two YES probes for the gate, then a
        # PERMISSION_DENIED reply for Manager.Suspend itself.
        self._runner._results[:] = [
            (0, CAN_SUSPEND_YES, ""),
            (0, CAN_POWEROFF_YES, ""),
            (1, "", PERMISSION_DENIED),
        ]
        resp = self._client.post("/api/system/power/suspend")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("inhibited", resp.json().get("detail", ""))

    def test_power_off_service_unknown_is_503_after_strict_yes_gate(self):
        # Override the queue so two YES probes feed the gate and the
        # third call -- Manager.PowerOff -- meets a vanished login1.
        self._runner._results[:] = [
            (0, CAN_SUSPEND_YES, ""),
            (0, CAN_POWEROFF_YES, ""),
            (1, "", SERVICE_UNKNOWN),
        ]
        resp = self._client.post("/api/system/power/power-off")
        self.assertEqual(resp.status_code, 503)

    def test_power_off_fails_with_409_when_logind_unreachable(self):
        # Bypass the strict-yes gate: both probes return "unavailable"
        # because login1 is gone.  The action POST must be rejected
        # with 409 *before* dispatching Manager.PowerOff.
        self._runner._results[:] = [
            (1, "", SERVICE_UNKNOWN),
            (1, "", SERVICE_UNKNOWN),
        ]
        resp = self._client.post("/api/system/power/power-off")
        self.assertEqual(resp.status_code, 409)
        self.assertIn(
            "unavailable", resp.json().get("detail", "").lower()
        )
        action_invocations = [
            call for call in self._runner.calls
            if "Manager.PowerOff" in call[-2]
        ]
        self.assertEqual(action_invocations, [])

    def test_suspend_fails_with_409_when_logind_reports_challenge(self):
        # Bypass the UI menu fully (no Origin check failure: CLI caller)
        # and watch the POST endpoint refuse the action because the
        # current logind answer is not "yes".  The capability probe
        # returns "challenge" -- the actual dbus-send action is NOT
        # invoked because the strict-yes gate stops it before.
        # Two probe slots: CanSuspend and CanPowerOff.
        self._runner._results[:] = [
            (0, _can_reply("challenge"), ""),
            (0, _can_reply("challenge"), ""),
        ]
        resp = self._client.post("/api/system/power/suspend")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("challenge", resp.json().get("detail", ""))
        action_invocations = [
            call for call in self._runner.calls
            if "Manager.Suspend" in call[-2]
        ]
        self.assertEqual(action_invocations, [])

    def test_power_off_fails_with_409_when_logind_reports_inhibited(self):
        # If an inhibitor lock engages between page load and the
        # action, the POST refuses -- no execution of Manager.PowerOff
        # and no upgrade of privilege.
        self._runner._results[:] = [
            (0, _can_reply("inhibited"), ""),
            (0, _can_reply("inhibited"), ""),
        ]
        resp = self._client.post("/api/system/power/power-off")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("inhibited", resp.json().get("detail", ""))
        action_invocations = [
            call for call in self._runner.calls
            if "Manager.PowerOff" in call[-2]
        ]
        self.assertEqual(action_invocations, [])

    def test_suspend_fails_with_409_for_every_non_yes_logind_value(self):
        # Every value other than "yes" must be rejected at the POST
        # gate.  Walking the full logind vocabulary catches any future
        # variant the maintainer forgets to whitelist.
        for value in [
            "no",
            "na",
            "challenge",
            "inhibited",
            "inhibitor-blocked",
            "challenge-inhibitor-blocked",
        ]:
            # Two probe responses so the gate sees the strict-yes refusal
            # for the action that was called; the second slot only
            # matters when the first probe alone does NOT already trigger
            # the gate (defensive).
            self._runner._results[:] = [
                (0, _can_reply(value), ""),
                (0, _can_reply("yes"), ""),
            ]
            resp = self._client.post("/api/system/power/suspend")
            self.assertEqual(
                resp.status_code, 409,
                msg=f"value={value!r} must NOT let the action POST through",
            )
            self.assertIn(
                value, resp.json().get("detail", ""),
                msg=f"detail must surface the logind value {value!r} verbatim",
            )
            action_invocations = [
                call for call in self._runner.calls
                if "Manager.Suspend" in call[-2]
            ]
            self.assertEqual(
                action_invocations, [],
                msg=f"action must not dispatch when CanSuspend={value!r}",
            )

    def test_capabilities_timeout_returns_503(self):
        """A hung logind must not pin the FastAPI loop; the timeout
        wrapper translates the cancellation into HTTP 503."""

        async def hang(*args, timeout=0.0):
            self.calls = getattr(self, "calls", [])
            self.calls.append(tuple(args))
            await asyncio.sleep(60)
            # unreachable
            return power._SubprocessResult(0, "", "")

        backend = power.PowerBackend(runner=hang)
        previous = power.set_backend(backend)
        try:
            resp = self._client.get("/api/system/power")
        finally:
            power.set_backend(previous)
        self.assertEqual(resp.status_code, 503)
        self.assertIn("not reachable", resp.json().get("detail", ""))


class CrossSiteDefenseTests(unittest.TestCase):
    """Reject cross-site POST triggers of /api/system/power/{suspend,power-off}.

    The check is the only CSRF mitigation FXRoute introduces for power
    actions; it must pass legitimate same-origin browser requests while
    blocking trivial cross-site triggers from foreign pages opened in
    the same browser session.
    """

    OK_REPLY = "method_return time=1700000000.000000 serial=42 reply_serial=2\n"

    def setUp(self):
        # Pre-fill with YES capability probes plus a single OK action reply,
        # so tests that expect a successful cross-site-clean POST can
        # complete the GET + POST surfaces with no extra wiring.
        self._runner = _ScriptedRunner(
            [(0, CAN_SUSPEND_YES, ""), (0, CAN_POWEROFF_YES, ""), (0, self.OK_REPLY, "")]
            + [(0, CAN_SUSPEND_YES, ""), (0, CAN_POWEROFF_YES, ""), (0, self.OK_REPLY, "")]
            * 16
        )
        self._previous = power.set_backend(power.PowerBackend(runner=self._runner))

    def tearDown(self):
        power.set_backend(self._previous)

    def _client_with_origin(self, base_url, origin_value):
        return TestClient(
            main_module.app,
            base_url=base_url,
            headers={"origin": origin_value},
        )

    def _client_with_referer(self, base_url, referer_value):
        return TestClient(
            main_module.app,
            base_url=base_url,
            headers={"referer": referer_value},
        )

    def test_suspend_accepts_same_origin_browser_request(self):
        client = self._client_with_origin(
            "http://fxroute.local:8000", "http://fxroute.local:8000"
        )
        resp = client.post("/api/system/power/suspend")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "suspended")

    def test_suspend_accepts_origin_with_matching_explicit_port(self):
        # Origin: http://fxroute.lan:8000 must match a request hitting
        # fxroute.lan on port 8000.
        client = self._client_with_origin(
            "http://fxroute.local:8000", "http://fxroute.local:8000"
        )
        resp = client.post("/api/system/power/suspend")
        self.assertEqual(resp.status_code, 200)

    def test_suspend_accepts_default_port_origin_when_server_uses_default(self):
        # When FXRoute listens on the protocol default port (80 for HTTP,
        # 443 for HTTPS), browsers send ``http://host`` with no port and
        # the Origin header must still compare as same-origin.
        client = self._client_with_origin(
            "http://fxroute.local", "http://fxroute.local"
        )
        resp = client.post("/api/system/power/suspend")
        self.assertEqual(resp.status_code, 200)

    def test_suspend_rejects_origin_with_wrong_port(self):
        # Same hostname, different port = same-policy cross-origin.
        client = self._client_with_origin(
            "http://fxroute.local:8000", "http://fxroute.local:9000"
        )
        resp = client.post("/api/system/power/suspend")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._runner.calls, [])

    def test_suspend_rejects_cross_origin_request(self):
        client = self._client_with_origin(
            "http://fxroute.local:8000", "https://evil.example.com"
        )
        resp = client.post("/api/system/power/suspend")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Cross-site", resp.json().get("detail", ""))
        # And the power backend must NOT have been called.
        self.assertEqual(self._runner.calls, [])

    def test_suspend_rejects_origin_null(self):
        # Browsers emit Origin: null for sandboxed documents; refuse those.
        client = self._client_with_origin(
            "http://fxroute.local:8000", "null"
        )
        resp = client.post("/api/system/power/suspend")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._runner.calls, [])

    def test_suspend_rejects_x_forwarded_host_mismatch(self):
        # X-Forwarded-Host/Port/Proto from the trusted reverse proxy
        # must override the test client's base URL.  Origin sent at the
        # FXRoute LAN hostname on the frontend port must be accepted.
        client = TestClient(
            main_module.app,
            base_url="http://10.0.0.5:8000",
            headers={
                "origin": "http://fxroute.lan",
                "x-forwarded-host": "fxroute.lan",
                "x-forwarded-port": "80",
                "x-forwarded-proto": "http",
            },
        )
        resp = client.post("/api/system/power/suspend")
        self.assertEqual(resp.status_code, 200)

    def test_suspend_rejects_x_forwarded_host_distinct_from_origin(self):
        # A reverse proxy that misroutes (or a forged X-Forwarded-Host)
        # must not let the cross-origin request reach the action.
        client = TestClient(
            main_module.app,
            base_url="http://10.0.0.5:8000",
            headers={
                "origin": "https://evil.example.com",
                "x-forwarded-host": "fxroute.lan",
                "x-forwarded-port": "443",
                "x-forwarded-proto": "https",
            },
        )
        resp = client.post("/api/system/power/suspend")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._runner.calls, [])

    def test_power_off_rejects_cross_origin_referer(self):
        client = self._client_with_referer(
            "http://fxroute.local:8000", "https://evil.example.com/exploit.html"
        )
        resp = client.post("/api/system/power/power-off")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._runner.calls, [])

    def test_power_off_accepts_same_origin_referer(self):
        client = self._client_with_referer(
            "http://fxroute.local:8000",
            "http://fxroute.local:8000/library",
        )
        resp = client.post("/api/system/power/power-off")
        self.assertEqual(resp.status_code, 200)

    def test_power_off_accepts_cli_caller_without_origin_or_referer(self):
        # The TestClient default does not send Origin/Referer; this models
        # a curl / systemd / script caller on the LAN.  Must still succeed.
        client = TestClient(main_module.app, base_url="http://fxroute.local:8000")
        resp = client.post("/api/system/power/power-off")
        self.assertEqual(resp.status_code, 200)

    def test_power_off_rejects_origin_with_wrong_port(self):
        client = self._client_with_origin(
            "http://fxroute.local:8000", "http://fxroute.local:9000"
        )
        resp = client.post("/api/system/power/power-off")
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
