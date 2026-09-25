#!/usr/bin/env python3
"""HTTP races between provider administration and system maintenance."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import main
import streaming.api as streaming_api
import system_update


INSTALL = "/api/streaming/providers/qobuz/install"
UNINSTALL = "/api/streaming/providers/qobuz/uninstall"
SERVICE = "/api/streaming/providers/qobuz/service/restart"
UPDATE = "/api/system/update"


class _ServiceProcess:
    returncode = 0

    async def communicate(self):
        return b"", b""


class ProviderAdminGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://testserver"
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_install_blocks_install_uninstall_service_and_update_without_waiting(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def installer(script, label, *args):
            calls.append(label)
            entered.set()
            await release.wait()
            return {"stdout": "ok", "stderr": "", "returncode": 0}

        async def forbidden_exec(*args, **kwargs):
            raise AssertionError("systemctl spawned while provider install is active")

        with mock.patch.object(streaming_api, "_run_provider_installer_op", new=installer), \
             mock.patch.object(streaming_api.asyncio, "create_subprocess_exec", new=forbidden_exec):
            first = asyncio.create_task(self.client.post(INSTALL))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                for route in (INSTALL, UNINSTALL, SERVICE, UPDATE):
                    with self.subTest(route=route):
                        response = await asyncio.wait_for(self.client.post(route), 2)
                        self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(calls, ["qobuz install"])
            finally:
                release.set()
                response = await first
        self.assertEqual(response.status_code, 200, response.text)

    async def test_service_blocks_installer_then_releases_guard(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def service_exec(*args, **kwargs):
            entered.set()
            await release.wait()
            return _ServiceProcess()

        with mock.patch.object(streaming_api.asyncio, "create_subprocess_exec", new=service_exec), \
             mock.patch.object(streaming_api, "_run_provider_installer_op", new=mock.AsyncMock(return_value={"stdout": "", "stderr": "", "returncode": 0})) as installer:
            first = asyncio.create_task(self.client.post(SERVICE))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                for route in (INSTALL, UNINSTALL, SERVICE):
                    response = await asyncio.wait_for(self.client.post(route), 2)
                    self.assertEqual(response.status_code, 409, response.text)
                installer.assert_not_awaited()
            finally:
                release.set()
                response = await first
            self.assertEqual(response.status_code, 200, response.text)
            response = await self.client.post(UNINSTALL)
            self.assertEqual(response.status_code, 200, response.text)

    async def test_uninstall_blocks_service_and_install_and_failure_releases_guard(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def failing_uninstall(script, label, *args):
            if "uninstall" in label:
                entered.set()
                await release.wait()
                raise RuntimeError("installer failed")
            return {"stdout": "", "stderr": "", "returncode": 0}

        with mock.patch.object(streaming_api, "_run_provider_installer_op", new=failing_uninstall):
            first = asyncio.create_task(self.client.post(UNINSTALL))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                for route in (INSTALL, SERVICE, UNINSTALL):
                    response = await asyncio.wait_for(self.client.post(route), 2)
                    self.assertEqual(response.status_code, 409, response.text)
            finally:
                release.set()
                with self.assertRaises(RuntimeError):
                    await first
            response = await self.client.post(INSTALL)
            self.assertEqual(response.status_code, 200, response.text)

    async def test_cancelled_install_releases_guard(self):
        entered = asyncio.Event()
        waiting = asyncio.Event()

        async def installer(*args):
            entered.set()
            await waiting.wait()

        with mock.patch.object(streaming_api, "_run_provider_installer_op", new=installer):
            first = asyncio.create_task(self.client.post(INSTALL))
            await asyncio.wait_for(entered.wait(), 2)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
        with mock.patch.object(streaming_api, "_run_provider_installer_op", new=mock.AsyncMock(return_value={"stdout": "", "stderr": "", "returncode": 0})):
            response = await self.client.post(INSTALL)
        self.assertEqual(response.status_code, 200, response.text)

    async def test_update_blocks_provider_and_deferred_restart_blocks_until_enqueued(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        restarting = asyncio.Event()
        restart_release = asyncio.Event()

        async def update_script(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"returncode": 0, "stdout": system_update.UPDATE_OUTPUT_FAST_FORWARD_MARKER, "stderr": ""}

        async def restart(*args, **kwargs):
            restarting.set()
            await restart_release.wait()

        with mock.patch.object(system_update, "run_update_script", new=update_script), \
             mock.patch.object(system_update, "restart_service_after_response", new=restart), \
             mock.patch.object(streaming_api, "_run_provider_installer_op", new=mock.AsyncMock(return_value={"stdout": "", "stderr": "", "returncode": 0})) as installer:
            update_task = asyncio.create_task(self.client.post(UPDATE))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                for route in (INSTALL, UNINSTALL, SERVICE):
                    response = await asyncio.wait_for(self.client.post(route), 2)
                    self.assertEqual(response.status_code, 409, response.text)
                release.set()
                result = await update_task
                self.assertEqual(result.status_code, 200, result.text)
                self.assertTrue(result.json()["restart_scheduled"])
                await asyncio.wait_for(restarting.wait(), 3)
                response = await self.client.post(INSTALL)
                self.assertEqual(response.status_code, 409, response.text)
                installer.assert_not_awaited()
            finally:
                release.set()
                restart_release.set()
                await update_task
                await asyncio.sleep(0)  # allow deferred restart task to release the reservation
            response = await self.client.post(INSTALL)
            self.assertEqual(response.status_code, 200, response.text)


if __name__ == "__main__":
    unittest.main()
