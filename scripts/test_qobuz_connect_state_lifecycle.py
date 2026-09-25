# SPDX-License-Identifier: AGPL-3.0-only
"""Qobuz connect_state lifecycle: uninstall/service transitions reset it.

Regression tests for the stale-renderer bug: ``connect_state`` stayed True
after the qbzd backend was uninstalled or its user service was stopped or
restarted, so the provider kept publishing the removed daemon as the active
renderer. Install/uninstall/service actions must reset the flag, and the
journal bootstrap must not repopulate it from pre-uninstall history.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from streaming.qobuz import connect_state  # noqa: E402


class QobuzConnectStateLifecycleTests(unittest.TestCase):
    def setUp(self):
        connect_state.reset()
        self.addCleanup(connect_state.reset)

    def test_reset_clears_active_flag(self):
        connect_state.set_device_active(True)
        self.assertTrue(connect_state.is_device_active())
        connect_state.reset()
        self.assertIsNone(connect_state.is_device_active())

    def test_api_uninstall_resets_qobuz_state(self):
        text = (Path(__file__).resolve().parents[1] / "streaming" / "api.py").read_text()
        uninstall = text.split("async def api_streaming_provider_uninstall")[1].split(
            "async def api_streaming_provider_service_action"
        )[0]
        self.assertIn('if provider_id == "qobuz"', uninstall)
        self.assertIn("connect_state.reset()", uninstall)

    def test_api_service_action_resets_qobuz_state(self):
        text = (Path(__file__).resolve().parents[1] / "streaming" / "api.py").read_text()
        service = text.split("async def api_streaming_provider_service_action")[1].split(
            "def _mdns_device_name"
        )[0]
        self.assertIn('if provider_id == "qobuz"', service)
        self.assertIn("connect_state.reset()", service)

    def test_api_install_resets_qobuz_state(self):
        text = (Path(__file__).resolve().parents[1] / "streaming" / "api.py").read_text()
        install = text.split("async def api_streaming_provider_install")[1].split(
            "async def api_streaming_provider_uninstall"
        )[0]
        self.assertIn('if provider_id == "qobuz"', install)
        self.assertIn("connect_state.reset()", install)

    def test_uninstall_lifecycle_clears_true_flag(self):
        # End-to-end through the real uninstall handler with a stubbed installer.
        import main as main_module
        from fastapi.testclient import TestClient

        import streaming.api as streaming_api

        async def installer_ok(script, label, *args):
            return {"returncode": 0, "stdout": "done", "stderr": ""}

        connect_state.set_device_active(True)
        client = TestClient(main_module.app)
        with mock.patch.object(streaming_api, "_run_provider_installer_op", new=installer_ok):
            response = client.post("/api/streaming/providers/qobuz/uninstall", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(connect_state.is_device_active())

    def test_service_restart_lifecycle_clears_true_flag(self):
        import main as main_module
        from fastapi.testclient import TestClient

        import streaming.api as streaming_api

        class Proc:
            def __init__(self, stdout=b"", returncode=0):
                self.returncode = returncode
                self._stdout = stdout

            async def communicate(self):
                return self._stdout, b""

        async def fake_exec(*args, **kwargs):
            argv = list(args)
            if argv[:3] == ["systemctl", "--user", "show"]:
                return Proc(stdout=b"active\n")
            if argv[:2] == ["systemctl", "--user"]:
                return Proc()
            raise AssertionError(f"unexpected subprocess: {argv}")

        connect_state.set_device_active(True)
        client = TestClient(main_module.app)
        with mock.patch.object(streaming_api.asyncio, "create_subprocess_exec", new=fake_exec):
            response = client.post("/api/streaming/providers/qobuz/service/restart", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(connect_state.is_device_active())


class QobuzBootstrapGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_skips_history_when_qbzd_not_installed(self):
        from playback.qbzd_volume_watch import QobuzVolumeWatch, QobuzVolumeWatchDependencies

        seen = []
        history = '[QConnect] SET_ACTIVE payload={"active":true}\n'

        class HistoryProc:
            returncode = 0

            async def communicate(self):
                return history.encode(), b""

        async def spawn(*args, **kwargs):
            return HistoryProc()

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=lambda value: None,
                current_master=lambda: 37,
                on_device_active=lambda value: seen.append(value),
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=spawn), mock.patch(
            "streaming.qobuz.backend.qbzd_installed", return_value=False
        ):
            await watch._bootstrap_device_state()
        self.assertEqual(seen, [])

    async def test_bootstrap_recovers_history_when_qbzd_installed(self):
        from playback.qbzd_volume_watch import QobuzVolumeWatch, QobuzVolumeWatchDependencies

        seen = []
        history = '[QConnect] SET_ACTIVE payload={"active":true}\n'

        class HistoryProc:
            returncode = 0

            async def communicate(self):
                return history.encode(), b""

        async def spawn(*args, **kwargs):
            return HistoryProc()

        watch = QobuzVolumeWatch(
            QobuzVolumeWatchDependencies(
                is_active=lambda: True,
                apply_volume_value=lambda value: None,
                current_master=lambda: 37,
                on_device_active=lambda value: seen.append(value),
            ),
            debounce_seconds=0.0,
        )
        with mock.patch("asyncio.create_subprocess_exec", new=spawn), mock.patch(
            "streaming.qobuz.backend.qbzd_installed", return_value=True
        ):
            await watch._bootstrap_device_state()
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
