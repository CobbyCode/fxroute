# SPDX-License-Identifier: AGPL-3.0-only

"""Settings -> Providers admin surface tests.

Covers the persisted activation store, the admin/enabled endpoints, the
device-name endpoints and the installer flags they drive:
* streaming.activation round-trip + defaults
* /api/streaming/providers/admin payload shape (enabled + device_name)
* enabled toggle endpoint validation and persistence
* device-name validation (installer hostname rule) and no-op path
* install.sh --providers-only / --with-lan-name / --with-caddy / --device-name
* uninstall.sh --provider scoped removal
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INSTALL_SH = ROOT / "install.sh"
UNINSTALL_SH = ROOT / "uninstall.sh"
ISO_FIRST_BOOT = ROOT / "iso" / "scripts" / "first-boot-install.sh"
ARMBIAN_FIRST_BOOT = ROOT / "armbian" / "first-boot-install.sh"

import streaming.activation as activation  # noqa: E402


class ActivationStoreTests(unittest.TestCase):
    """The enabled flag persists per provider and defaults to enabled."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict("os.environ", {"FXROUTE_CONFIG_DIR": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_missing_state_means_enabled(self):
        self.assertTrue(activation.is_enabled("spotify"))

    def test_set_and_read_round_trip(self):
        activation.set_enabled("qobuz", False)
        self.assertFalse(activation.is_enabled("qobuz"))
        self.assertTrue(activation.is_enabled("tidal"))

    def test_overwrite(self):
        activation.set_enabled("tidal", False)
        activation.set_enabled("tidal", True)
        self.assertTrue(activation.is_enabled("tidal"))

    def test_enabled_map_covers_known_ids(self):
        result = activation.enabled_map(["spotify", "qobuz", "tidal"])
        self.assertEqual(set(result), {"spotify", "qobuz", "tidal"})
        self.assertTrue(all(result.values()))

    def test_corrupt_state_file_falls_back_to_enabled(self):
        path = Path(self._tmp.name) / "provider-activation.json"
        path.write_text("{not json")
        self.assertTrue(activation.is_enabled("spotify"))


class _AdminClientBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict("os.environ", {"FXROUTE_CONFIG_DIR": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)
        import main as main_module
        from fastapi.testclient import TestClient

        self.main = main_module
        self.client = TestClient(main_module.app)


class ProviderAdminEndpointTests(_AdminClientBase):
    def test_admin_payload_lists_all_providers_with_enabled(self):
        data = self.client.get("/api/streaming/providers/admin").json()
        ids = {p["id"] for p in data["providers"]}
        self.assertEqual(ids, {"spotify", "qobuz", "tidal"})
        for provider in data["providers"]:
            self.assertIn("installed", provider)
            self.assertIn("enabled", provider)
            self.assertIn("authenticated", provider)
        self.assertIsInstance(data["device_name"], str)
        self.assertTrue(data["device_name"])

    def test_enabled_toggle_round_trip(self):
        resp = self.client.post(
            "/api/streaming/providers/qobuz/enabled", json={"enabled": False}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["enabled"])
        self.assertFalse(activation.is_enabled("qobuz"))

        resp = self.client.post(
            "/api/streaming/providers/qobuz/enabled", json={"enabled": True}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(activation.is_enabled("qobuz"))

    def test_enabled_toggle_requires_boolean(self):
        resp = self.client.post(
            "/api/streaming/providers/qobuz/enabled", json={"enabled": "yes"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_enabled_toggle_unknown_provider_404(self):
        resp = self.client.post(
            "/api/streaming/providers/napster/enabled", json={"enabled": True}
        )
        self.assertEqual(resp.status_code, 404)

    def test_discovery_includes_enabled_flag(self):
        self.client.post(
            "/api/streaming/providers/tidal/enabled", json={"enabled": False}
        )
        data = self.client.get("/api/streaming/providers/discovery").json()
        flags = {p["id"]: p.get("enabled") for p in data["providers"]}
        self.assertFalse(flags["tidal"])
        self.assertTrue(flags["spotify"])


class DeviceNameEndpointTests(_AdminClientBase):
    def test_get_device_name(self):
        data = self.client.get("/api/system/device-name").json()
        self.assertIn("hostname", data)
        self.assertIn("can_change", data)
        self.assertTrue(data["hostname"])

    def test_set_device_name_rejects_invalid(self):
        for bad in ("UPPER", "-lead", "trail-", "a" * 70, "", "with space"):
            resp = self.client.post("/api/system/device-name", json={"hostname": bad})
            self.assertEqual(resp.status_code, 400, msg=f"hostname={bad!r}")

    def test_set_device_name_rejects_localhost(self):
        resp = self.client.post("/api/system/device-name", json={"hostname": "localhost"})
        self.assertEqual(resp.status_code, 400)

    def test_set_device_name_noop_when_unchanged(self):
        with mock.patch.object(self.main, "shutil", wraps=self.main.shutil) as shutil_mock:
            # hostnamectl missing would 503; keep it available but verify no
            # subprocess runs for a no-op rename.
            shutil_mock.which.return_value = "/usr/bin/hostnamectl"
            with mock.patch.object(self.main, "_mdns_device_name", return_value="fxroute"):
                with mock.patch.object(self.main.asyncio, "create_subprocess_exec") as exec_mock:
                    resp = self.client.post("/api/system/device-name", json={"hostname": "fxroute"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["changed"])
        exec_mock.assert_not_called()


class InstallerProviderOnlyTests(unittest.TestCase):
    """install.sh must expose a providers-only path and image auto flags."""

    @classmethod
    def setUpClass(cls):
        cls.install = INSTALL_SH.read_text()
        cls.uninstall = UNINSTALL_SH.read_text()

    def test_install_defines_providers_only_mode(self):
        self.assertIn("PROVIDERS_ONLY_MODE=0", self.install)
        self.assertIn("--providers-only", self.install)
        self.assertIn("main_providers_only() {", self.install)

    def test_providers_only_branch_runs_before_full_install(self):
        # main() must dispatch to main_providers_only before the full install
        # pipeline (capture_lan_comfort_baseline and beyond).
        body = self.install[self.install.index("main() {"):]
        idx = body.index("main_providers_only")
        idx_full = body.index("capture_lan_comfort_baseline")
        self.assertLess(idx, idx_full)

    def test_providers_only_main_reuses_existing_flows(self):
        body = self.install[self.install.index("main_providers_only() {"):]
        body = body[:body.index("\nmain() {")]
        self.assertIn("configure_optional_streaming", body)
        self.assertIn("write_install_state", body)
        # Must not touch the service or the venv.
        self.assertNotIn("write_service_unit", body)
        self.assertNotIn("setup_python_env", body)

    def test_providers_only_respects_recorded_install_root(self):
        self.assertIn("PROVIDERS_ONLY_MODE -eq 1 && $INSTALL_ROOT_EXPLICIT -eq 0", self.install)
        self.assertIn("FXROUTE_INSTALL_ROOT=", self.install)

    def test_image_flags_exist(self):
        self.assertIn("--with-lan-name", self.install)
        self.assertIn("--with-caddy", self.install)
        self.assertIn("--device-name", self.install)
        self.assertIn("AUTO_LAN_NAME=0", self.install)
        self.assertIn("AUTO_CADDY=0", self.install)

    def test_lan_name_auto_branch_is_non_interactive(self):
        self.assertIn("AUTO_LAN_NAME -eq 1", self.install)
        self.assertIn("AUTO_DEVICE_NAME", self.install)

    def test_caddy_auto_branch_skips_tty_gate(self):
        body = self.install[self.install.index("offer_optional_caddy_proxy() {"):]
        body = body[:body.index("\nmain() {")]
        # The auto branch must run without a TTY; the interactive path keeps
        # its gate.
        self.assertIn("AUTO_CADDY -eq 1", body)
        self.assertLess(body.index("AUTO_CADDY -eq 1"), body.index("[[ -t 0 && -t 1 ]] || return 0"))

    def test_uninstall_defines_scoped_provider_removal(self):
        self.assertIn("--provider", self.uninstall)
        self.assertIn("remove_single_provider() {", self.uninstall)
        self.assertIn("clear_provider_ownership_state() {", self.uninstall)
        self.assertIn('case "$provider" in', self.uninstall)
        self.assertIn("remove_owned_qbzd", self.uninstall)
        self.assertIn("remove_owned_spotifyd", self.uninstall)
        self.assertIn("remove_owned_tidal_dependency", self.uninstall)

    def test_scoped_removal_clears_state_for_provider(self):
        body = self.uninstall[self.uninstall.index("clear_provider_ownership_state() {"):]
        body = body[:body.index("remove_single_provider()")]
        self.assertIn('payload = json.loads(path.read_text())', body)
        self.assertIn('providers.pop(provider, None)', body)


class ImageFirstBootNamingTests(unittest.TestCase):
    """Image first-boot must pass a unique stable name + auto LAN/Caddy."""

    @staticmethod
    def _installer_call(text):
        idx = text.index("--source")
        return text[idx:text.index("--yes")]

    def test_iso_first_boot_uses_auto_flags(self):
        script = ISO_FIRST_BOOT.read_text()
        call = self._installer_call(script)
        self.assertIn("--with-lan-name", call)
        self.assertIn("--with-caddy", call)
        self.assertIn("--device-name", call)

    def test_iso_device_name_is_machine_id_derived(self):
        script = ISO_FIRST_BOOT.read_text()
        self.assertIn("derive_fxroute_device_name()", script)
        self.assertIn("/etc/machine-id", script)
        self.assertIn("fxroute-", script)

    def test_armbian_first_boot_uses_auto_flags(self):
        script = ARMBIAN_FIRST_BOOT.read_text()
        call = self._installer_call(script)
        self.assertIn("--with-lan-name", call)
        self.assertIn("--with-caddy", call)
        self.assertIn("--device-name", call)

    def test_armbian_device_name_is_machine_id_derived(self):
        script = ARMBIAN_FIRST_BOOT.read_text()
        self.assertIn("derive_fxroute_device_name()", script)
        self.assertIn("/etc/machine-id", script)
        self.assertIn("fxroute-", script)


if __name__ == "__main__":
    unittest.main()
