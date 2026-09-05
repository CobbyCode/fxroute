# SPDX-License-Identifier: AGPL-3.0-only

"""Settings -> Providers admin surface tests.

Covers the persisted activation store, the admin/enabled endpoints, the
device-name endpoints and the installer flags they drive:
* streaming.activation round-trip + defaults
* /api/streaming/providers/admin payload shape (enabled + device_name)
* enabled toggle endpoint validation and persistence
* device-name validation (installer hostname rule) and no-op path
* cross-site origin guard on the provider/Qobuz POST endpoints
* install.sh --providers-only / --with-lan-name / --with-caddy / --device-name
* uninstall.sh --provider scoped removal
"""

from __future__ import annotations

import pathlib
import re
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
        self._env = mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_state_lives_under_xdg_config_home(self):
        # The provider activation state must follow the same config home as
        # every other FXRoute config file (XDG_CONFIG_HOME/fxroute), not a
        # divergent FXROUTE_CONFIG_DIR/home fallback.
        expected = Path(self._tmp.name) / "fxroute" / activation.STATE_FILENAME
        self.assertEqual(activation._state_path(), expected)
        activation.set_enabled("qobuz", False)
        self.assertTrue(expected.exists())

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
        path = Path(self._tmp.name) / "fxroute" / activation.STATE_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertTrue(activation.is_enabled("spotify"))


class _AdminClientBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": self._tmp.name})
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

    def test_admin_payload_reports_device_name_capability(self):
        data = self.client.get("/api/streaming/providers/admin").json()
        self.assertIn("device_name_can_change", data)
        self.assertIsInstance(data["device_name_can_change"], bool)
        with mock.patch.object(self.main.shutil, "which", return_value=None):
            data = self.client.get("/api/streaming/providers/admin").json()
            self.assertFalse(data["device_name_can_change"])
        with mock.patch.object(self.main.shutil, "which", return_value="/usr/bin/hostnamectl"):
            data = self.client.get("/api/streaming/providers/admin").json()
            self.assertTrue(data["device_name_can_change"])

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


class ProviderEndpointOriginGuardTests(_AdminClientBase):
    """State-changing provider/Qobuz POSTs share the cross-site origin guard.

    Same defence as the power and device-name endpoints: a POST carrying a
    foreign (or "null") Origin must be rejected before any installer,
    systemctl, or credential side effect runs. Headerless calls (CLI/systemd)
    and same-origin browser calls stay allowed.
    """

    FOREIGN_ORIGIN = {"Origin": "https://evil.example.com"}
    NULL_ORIGIN = {"Origin": "null"}

    def test_cross_site_provider_actions_are_rejected(self):
        for url in (
            "/api/streaming/providers/qobuz/enabled",
            "/api/streaming/providers/qobuz/install",
            "/api/streaming/providers/qobuz/uninstall",
            "/api/streaming/providers/qobuz/service/restart",
        ):
            resp = self.client.post(url, json={}, headers=self.FOREIGN_ORIGIN)
            self.assertEqual(resp.status_code, 403, msg=url)

    def test_cross_site_qobuz_auth_actions_are_rejected(self):
        for url in (
            "/api/streaming/qobuz/auth/login",
            "/api/streaming/qobuz/auth/login/finish",
            "/api/streaming/qobuz/auth/login/cancel",
            "/api/streaming/qobuz/auth/logout",
        ):
            resp = self.client.post(url, json={}, headers=self.FOREIGN_ORIGIN)
            self.assertEqual(resp.status_code, 403, msg=url)

    def test_null_origin_is_rejected(self):
        resp = self.client.post(
            "/api/streaming/providers/qobuz/install",
            json={},
            headers=self.NULL_ORIGIN,
        )
        self.assertEqual(resp.status_code, 403)

    def test_cross_site_install_never_spawns_the_installer(self):
        with mock.patch.object(
            self.main, "_run_provider_installer_op", new_callable=mock.AsyncMock
        ) as op_mock:
            resp = self.client.post(
                "/api/streaming/providers/qobuz/install",
                json={},
                headers=self.FOREIGN_ORIGIN,
            )
        self.assertEqual(resp.status_code, 403)
        op_mock.assert_not_called()

    def test_cross_site_service_action_never_spawns_systemctl(self):
        with mock.patch.object(self.main.asyncio, "create_subprocess_exec") as exec_mock:
            resp = self.client.post(
                "/api/streaming/providers/qobuz/service/restart",
                json={},
                headers=self.FOREIGN_ORIGIN,
            )
        self.assertEqual(resp.status_code, 403)
        exec_mock.assert_not_called()

    def test_cross_site_enabled_toggle_changes_nothing(self):
        self.assertTrue(activation.is_enabled("qobuz"))
        resp = self.client.post(
            "/api/streaming/providers/qobuz/enabled",
            json={"enabled": False},
            headers=self.FOREIGN_ORIGIN,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(activation.is_enabled("qobuz"))

    def test_same_origin_post_is_allowed(self):
        resp = self.client.post(
            "/api/streaming/providers/qobuz/enabled",
            json={"enabled": False},
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(activation.is_enabled("qobuz"))


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

    def test_providers_only_resolves_privilege_before_privileged_steps(self):
        # The single non-interactive bootstrap must run before any apt-get /
        # systemctl / usermod call of the providers-only run, and the SUDO_CMD
        # selection must switch to "sudo -n" only for that verified mode.
        body = self.install[self.install.index("main_providers_only() {"):]
        body = body[:body.index("\nmain() {")]
        self.assertIn("ensure_provider_privilege_escalation", body)
        self.assertLess(
            body.index("ensure_provider_privilege_escalation"),
            body.index("configure_optional_streaming"),
        )
        sudo_body = self.install[self.install.index("choose_sudo() {"):]
        sudo_body = sudo_body[:sudo_body.index("\n}\n")]
        self.assertIn("PROVIDER_PRIVILEGE_MODE", sudo_body)
        self.assertIn("sudo -n", sudo_body)

    def test_provider_privilege_rule_is_scoped_to_providers_only(self):
        # Central allow-list: exactly one sudoers entry, this script with
        # --providers-only only. The rule body must contain a single NOPASSWD
        # command line and no separate allow-listed binaries or ALL grant.
        rule = self.install[self.install.index("provider_privilege_rule_content() {"):]
        rule = rule[:rule.index("\n}\n")]
        self.assertIn("--providers-only", rule)
        self.assertIn("NOPASSWD:", rule)
        printf_lines = [line for line in rule.splitlines() if line.strip().startswith("printf")]
        self.assertEqual(len(printf_lines), 1)
        command = printf_lines[0]
        for forbidden in ("apt-get", "systemctl", "usermod", "ALL=(ALL)", "ALL = (ALL)"):
            self.assertNotIn(forbidden, command)

    def test_provider_privilege_state_is_recorded_for_uninstall(self):
        self.assertIn("privilege_escalation", self.install)
        self.assertIn("sudoers_sha256", self.install)
        self.assertIn("providers.privilege_escalation.installed_by_fxroute", self.install)
        self.assertIn("remove_provider_privilege_escalation", self.uninstall)
        self.assertIn("fxroute-providers", self.uninstall)

    def test_provider_install_endpoint_uses_noninteractive_sudo(self):
        main_text = (ROOT / "main.py").read_text()
        body = main_text.split("async def _run_provider_installer_op")[1]
        body = body.split("\n@app.")[0]
        self.assertIn('"sudo", "-n"', body)
        self.assertIn("a password is required", body)
        self.assertIn("503", body)

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


class QobuzSetupCompletionTests(unittest.TestCase):
    """A present-but-never-set-up qbzd must stay repairable via the installer.

    Binary present + daemon down (manually placed binary or interrupted
    install) used to dead-end: Settings offered Connect/Uninstall/Restart,
    while Install was only offered when not installed. The completing run
    (install.sh --providers-only --qobuz) adopts the binary, pins the volume
    contract, creates/starts the user service and records the install state.
    """

    @classmethod
    def setUpClass(cls):
        cls.app_js = (ROOT / "static" / "app.js").read_text()
        cls.main = (ROOT / "main.py").read_text()

    def test_qobuz_offers_complete_setup_while_daemon_down(self):
        match = re.search(
            r"if \(!provider\.available\) \{(.*?)data-provider-service=\"restart\"",
            self.app_js,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "qobuz down-state must render recovery actions")
        self.assertIn("data-provider-install", match.group(1))
        self.assertIn("Complete setup", match.group(1))

    def test_complete_setup_reuses_provider_install_flow(self):
        # The button must use the same hook the Install wiring consumes, so
        # it rides runProviderInstall -> POST .../install -> providers-only.
        self.assertIn("querySelectorAll('[data-provider-install]')", self.app_js)
        self.assertIn("runProviderInstall(button.getAttribute(", self.app_js)

    def test_install_endpoint_allows_completing_run_when_installed(self):
        body = self.main.split("async def api_streaming_provider_install")[1]
        body = body.split("\n@app.")[0]
        self.assertIn('"--providers-only"', body)
        self.assertIn('"qobuz": "--qobuz"', body)
        self.assertNotIn("already installed", body)


class TidalLoginParityTests(unittest.TestCase):
    """TIDAL browser login must mirror the Qobuz login dialog flow.

    Same dialog structure, same step order, same UI states (connect,
    external sign-in, success, error, disconnect). Only the provider
    endpoints differ; the PKCE/auth backend itself stays untouched.
    """

    @classmethod
    def setUpClass(cls):
        cls.index = (ROOT / "static" / "index.html").read_text()
        cls.app_js = (ROOT / "static" / "app.js").read_text()
        cls.streaming_js = (ROOT / "static" / "streaming.js").read_text()
        cls.main = (ROOT / "main.py").read_text()

    def _panel(self, provider):
        match = re.search(
            rf'<div id="{provider}-login-panel".*?</section>\s*</div>',
            self.index,
            re.DOTALL,
        )
        self.assertIsNotNone(match, f"{provider} login panel is missing")
        return match.group(0)

    def test_login_panels_share_dialog_structure(self):
        for panel in (self._panel("qobuz"), self._panel("tidal")):
            for token in (
                'class="manage-overlay-backdrop"',
                'role="dialog"',
                'class="effects-stack"',
                'class="radio-manage-section"',
                'class="url-input"',
                'aria-live="polite">Waiting for sign-in…</p>',
                ">Connect</button>",
                ">Cancel</button>",
            ):
                self.assertIn(token, panel)
        for element_id in (
            "tidal-login-panel",
            "tidal-login-title",
            "tidal-login-close",
            "tidal-login-url",
            "tidal-login-open",
            "tidal-login-copy",
            "tidal-login-redirect",
            "tidal-login-status",
            "tidal-login-finish",
            "tidal-login-cancel",
        ):
            self.assertIn(f'id="{element_id}"', self.index)

    def test_tidal_modal_uses_pkce_endpoints_with_qobuz_states(self):
        self.assertIn("fetch('/api/streaming/tidal/auth/pkce'", self.app_js)
        self.assertIn("fetch('/api/streaming/tidal/auth/pkce/finish'", self.app_js)
        self.assertIn("body: JSON.stringify({ redirect_url: pasted })", self.app_js)
        for token in (
            "setTidalLoginStatus('Waiting for sign-in…')",
            "setTidalLoginStatus('Completing the TIDAL login…', 'busy')",
            "showToast('TIDAL connected', 'success')",
            "showToast('Sign-in link copied.', 'success')",
        ):
            self.assertIn(token, self.app_js)
        # Enter commits, Cancel/Close only close: PKCE keeps no cancelable
        # server listener, unlike the qbzd one-shot process.
        self.assertIn("void finishTidalLoginFromModal()", self.app_js)
        self.assertIn(
            "elements.tidalLoginCancelBtn?.addEventListener('click', () => closeTidalLoginModal())",
            self.app_js,
        )

    def test_settings_connect_opens_tidal_modal(self):
        match = re.search(
            r"querySelectorAll\('\[data-provider-tidal-login\]'\)(.*?)querySelectorAll\('\[data-provider-tidal-logout\]'\)",
            self.app_js,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        self.assertIn("void beginTidalLogin()", match.group(1))
        self.assertNotIn("switchTab('tidal')", match.group(1))
        self.assertIn("openTidalLogin: () => void beginTidalLogin()", self.app_js)

    def test_tab_primary_login_routes_to_modal_and_keeps_device_flow(self):
        self.assertIn("api.openTidalLogin", self.streaming_js)
        self.assertIn("renderTidalDevice", self.streaming_js)
        self.assertIn("renderTidalPkce", self.streaming_js)

    def test_tidal_auth_backend_is_untouched(self):
        for token in (
            "async def api_tidal_pkce_login_url",
            "async def api_tidal_finish_pkce_login",
            "async def api_tidal_logout",
            "finish_pkce_login(redirect_url)",
        ):
            self.assertIn(token, self.main)


if __name__ == "__main__":
    unittest.main()
