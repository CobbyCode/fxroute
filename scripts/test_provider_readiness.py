# SPDX-License-Identifier: AGPL-3.0-only
"""Provider readiness gating for install/update and service actions.

Regression tests for three fixed false-success reports:
* a successful installer run (exit 0) reported ok=true even when the
  required spotifyd/qbzd service restart failed or the unit never reached
  active state (install doubles as update in the Settings UI);
* a TIDAL install reported ok=true even when tidalapi stayed unimportable;
* a service start/restart reported ok=true on systemctl exit 0 without
  reading back whether the unit actually reached ActiveState=active.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import streaming.api as streaming_api  # noqa: E402
from streaming.tidal.provider import TidalProvider  # noqa: E402


class _Proc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode


async def _installer_ok(script, label, *args):
    return {"returncode": 0, "stdout": "done", "stderr": ""}


def _exec_fake(testcase, active_state="active", action_returncode=0, calls=None):
    """Fake create_subprocess_exec dispatching on argv.

    systemctl action calls succeed with action_returncode; state readbacks
    (``show -p ActiveState``) report active_state. Anything else fails the
    test: install/service paths must not spawn anything unexpected.
    """

    async def fake_exec(*args, **kwargs):
        argv = list(args)
        if calls is not None:
            calls.append(argv)
        if argv[:3] == ["systemctl", "--user", "show"]:
            testcase.assertIn("ActiveState", argv)
            return _Proc(returncode=0, stdout=(active_state + "\n").encode())
        if argv[:2] == ["systemctl", "--user"]:
            return _Proc(returncode=action_returncode, stderr=b"fake failure")
        raise AssertionError(f"unexpected subprocess: {argv}")

    return fake_exec


class _ClientBase(unittest.TestCase):
    def setUp(self):
        import main as main_module
        from fastapi.testclient import TestClient

        self.client = TestClient(main_module.app)


class ProviderInstallReadinessTests(_ClientBase):
    """Installer exit 0 alone must not report success."""

    def _post_install(self, provider_id, active_state="active"):
        calls: list = []
        with mock.patch.object(streaming_api, "_run_provider_installer_op", new=_installer_ok), \
            mock.patch.object(
                streaming_api.asyncio,
                "create_subprocess_exec",
                new=_exec_fake(self, active_state=active_state, calls=calls),
            ):
            response = self.client.post(
                f"/api/streaming/providers/{provider_id}/install", json={}
            )
        return response, calls

    def test_spotify_update_with_failed_restart_is_an_error(self):
        # Successful binary step, spotifyd restart failed -> no success status.
        response, calls = self._post_install("spotify", active_state="inactive")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotEqual(response.json().get("ok"), True)
        self.assertIn("spotifyd.service", response.json()["detail"])
        self.assertTrue(
            any(argv[:3] == ["systemctl", "--user", "show"] for argv in calls),
            "install must read back the service state",
        )

    def test_qobuz_install_with_dead_service_is_an_error(self):
        response, _ = self._post_install("qobuz", active_state="failed")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotEqual(response.json().get("ok"), True)
        self.assertIn("qbzd.service", response.json()["detail"])

    def test_spotify_install_with_ready_service_succeeds(self):
        response, _ = self._post_install("spotify", active_state="active")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["provider_id"], "spotify")

    def test_qobuz_install_with_ready_service_succeeds(self):
        response, _ = self._post_install("qobuz", active_state="active")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])


class TidalInstallReadinessTests(_ClientBase):
    """TIDAL has no service; the refreshed import verdict gates success."""

    def _post_install(self, installed):
        with mock.patch.object(streaming_api, "_run_provider_installer_op", new=_installer_ok), \
            mock.patch.object(TidalProvider, "is_installed", return_value=installed), \
            mock.patch.object(
                streaming_api.asyncio, "create_subprocess_exec",
                new=_exec_fake(self, active_state="active"),
            ):
            return self.client.post("/api/streaming/providers/tidal/install", json={})

    def test_tidal_install_without_importable_tidalapi_is_an_error(self):
        response = self._post_install(installed=False)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotEqual(response.json().get("ok"), True)

    def test_tidal_install_with_importable_tidalapi_succeeds(self):
        response = self._post_install(installed=True)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["installed"])


class ProviderServiceActionReadinessTests(_ClientBase):
    """systemctl exit 0 alone must not report start/restart success."""

    def _post_service(self, provider_id, action, active_state="active",
                      action_returncode=0):
        calls: list = []
        with mock.patch.object(
            streaming_api.asyncio,
            "create_subprocess_exec",
            new=_exec_fake(
                self,
                active_state=active_state,
                action_returncode=action_returncode,
                calls=calls,
            ),
        ):
            response = self.client.post(
                f"/api/streaming/providers/{provider_id}/service/{action}", json={}
            )
        return response, calls

    def test_start_with_exit_zero_but_inactive_service_is_an_error(self):
        response, calls = self._post_service("spotify", "start", active_state="inactive")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotEqual(response.json().get("ok"), True)
        self.assertIn("spotifyd.service", response.json()["detail"])
        self.assertTrue(
            any(argv[:3] == ["systemctl", "--user", "show"] for argv in calls),
            "start must read back the service state",
        )

    def test_restart_with_exit_zero_but_failed_service_is_an_error(self):
        response, _ = self._post_service("qobuz", "restart", active_state="failed")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotEqual(response.json().get("ok"), True)

    def test_successful_start_with_active_service_succeeds(self):
        response, _ = self._post_service("spotify", "start", active_state="active")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["unit"], "spotifyd.service")

    def test_successful_restart_with_active_service_succeeds(self):
        response, _ = self._post_service("qobuz", "restart", active_state="active")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])

    def test_failed_systemctl_still_fails_without_readback(self):
        response, calls = self._post_service(
            "spotify", "start", active_state="active", action_returncode=1
        )
        self.assertEqual(response.status_code, 500, response.text)
        self.assertFalse(
            any(argv[:3] == ["systemctl", "--user", "show"] for argv in calls),
            "no readback after a rejected start request",
        )

    def test_stop_stays_exit_code_gated(self):
        response, calls = self._post_service("spotify", "stop", active_state="active")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        self.assertFalse(
            any(argv[:3] == ["systemctl", "--user", "show"] for argv in calls),
            "stop keeps its previous exit-code contract",
        )


class SpotifydRestartBestEffortTests(unittest.IsolatedAsyncioTestCase):
    """The best-effort spotifyd restart is only a success when active."""

    async def _restart(self, action_returncode=0, active_state="active"):
        calls: list = []

        async def fake_exec(*args, **kwargs):
            argv = list(args)
            calls.append(argv)
            if argv[:3] == ["systemctl", "--user", "show"]:
                return _Proc(returncode=0, stdout=(active_state + "\n").encode())
            return _Proc(returncode=action_returncode)

        with mock.patch.object(streaming_api.asyncio, "create_subprocess_exec", new=fake_exec):
            return await streaming_api._restart_spotifyd_best_effort()

    async def test_restart_accepted_and_active_is_success(self):
        self.assertTrue(await self._restart(action_returncode=0, active_state="active"))

    async def test_restart_accepted_but_inactive_is_failure(self):
        self.assertFalse(await self._restart(action_returncode=0, active_state="inactive"))

    async def test_rejected_restart_is_failure(self):
        self.assertFalse(await self._restart(action_returncode=1, active_state="active"))


class ServiceStateReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_active_state_value(self):
        async def fake_exec(*args, **kwargs):
            self.assertEqual(args[2:6], ("show", "-p", "ActiveState", "--value"))
            return _Proc(returncode=0, stdout=b"active\n")

        with mock.patch.object(streaming_api.asyncio, "create_subprocess_exec", new=fake_exec):
            self.assertEqual(
                await streaming_api._read_user_service_active_state("spotifyd.service"),
                "active",
            )

    async def test_unreadable_state_returns_none(self):
        for proc in (_Proc(returncode=1, stdout=b"active\n"), _Proc(returncode=0, stdout=b"\n")):
            with self.subTest(returncode=proc.returncode):
                with mock.patch.object(
                    streaming_api.asyncio, "create_subprocess_exec",
                    new=mock.AsyncMock(return_value=proc),
                ):
                    self.assertIsNone(
                        await streaming_api._read_user_service_active_state("qbzd.service")
                    )

    async def test_spawn_failure_returns_none(self):
        with mock.patch.object(
            streaming_api.asyncio, "create_subprocess_exec",
            new=mock.AsyncMock(side_effect=OSError("no dbus")),
        ):
            self.assertIsNone(
                await streaming_api._read_user_service_active_state("qbzd.service")
            )


if __name__ == "__main__":
    unittest.main()
