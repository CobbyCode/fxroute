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
PROVIDER_HELPER = ROOT / "scripts" / "fxroute-provider-privileged"
ISO_FIRST_BOOT = ROOT / "iso" / "scripts" / "first-boot-install.sh"
ARMBIAN_FIRST_BOOT = ROOT / "armbian" / "first-boot-install.sh"

import installer_contract as provider_contract  # noqa: E402
import streaming.activation as activation  # noqa: E402
import streaming.api as streaming_api  # noqa: E402


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
        with mock.patch.object(streaming_api.shutil, "which", return_value=None):
            data = self.client.get("/api/streaming/providers/admin").json()
            self.assertFalse(data["device_name_can_change"])
        with mock.patch.object(streaming_api.shutil, "which", return_value="/usr/bin/hostnamectl"):
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
            streaming_api, "_run_provider_installer_op", new_callable=mock.AsyncMock
        ) as op_mock:
            resp = self.client.post(
                "/api/streaming/providers/qobuz/install",
                json={},
                headers=self.FOREIGN_ORIGIN,
            )
        self.assertEqual(resp.status_code, 403)
        op_mock.assert_not_called()

    def test_cross_site_service_action_never_spawns_systemctl(self):
        with mock.patch.object(streaming_api.asyncio, "create_subprocess_exec") as exec_mock:
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
            with mock.patch.object(streaming_api, "_mdns_device_name", return_value="fxroute"):
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

    def test_providers_only_needs_helper_and_no_sudo(self):
        # The providers-only path must never call sudo itself: the only
        # root steps are the fixed helper actions (provider_privileged).
        # A missing helper fails closed with a rerun-the-installer error.
        body = self.install[self.install.index("main_providers_only() {"):]
        body = body[:body.index("\nmain() {")]
        self.assertIn("provider_helper_usable", body)
        # The 503 trigger is the machine-readable contract marker, not the
        # operator-facing prose (which is free to change).
        self.assertIn("PROVIDER_CONTRACT_HELPER_MISSING", body)
        self.assertNotIn("SUDO_CMD", body)
        self.assertNotIn("choose_sudo", body)

    def test_provider_contract_module_is_the_single_source(self):
        # The shell-side markers must equal the Python contract module, so
        # the 503 path cannot drift between install.sh and streaming/api.py.
        self.assertEqual(provider_contract.MARKER_PREFIX, "PROVIDER_CONTRACT=")
        self.assertEqual(
            provider_contract.MARKER_HELPER_MISSING,
            provider_contract.MARKER_PREFIX + "helper-missing",
        )
        shell = self.install
        self.assertIn(f'PROVIDER_CONTRACT_PREFIX="{provider_contract.MARKER_PREFIX}"', shell)
        self.assertIn(f'PROVIDER_CONTRACT_HELPER_MISSING="${{PROVIDER_CONTRACT_PREFIX}}helper-missing"', shell)
        self.assertIn("verify_provider_contract_literals", shell)
        streaming_text = (ROOT / "streaming" / "api.py").read_text()
        self.assertIn("import installer_contract as provider_contract", streaming_text)
        # Prose must not be load-bearing anywhere: no side may grep for it.
        for text in (shell, streaming_text):
            self.assertNotIn('"Provider privilege helper unavailable"', text)

    def test_provider_contract_marker_shape(self):
        # The marker must stay a KEY=VALUE pair so log scrapers and tests
        # can rely on its shape.
        marker = provider_contract.MARKER_HELPER_MISSING
        self.assertIn("=", marker)
        self.assertNotIn(" ", marker)
        self.assertTrue(marker.split("=", 1)[0])
        self.assertTrue(marker.split("=", 1)[1])

    def test_provider_privilege_rule_allows_only_the_root_owned_helper(self):
        # sudoers must allow exactly the root-owned helper, nothing else:
        # no user-writable install.sh path, no wildcard on a script, no
        # bare apt/systemctl/usermod command.
        rule = self.install[self.install.index("provider_privilege_rule_content() {"):]
        rule = rule[:rule.index("\n}\n")]
        self.assertIn("NOPASSWD:", rule)
        self.assertIn("PROVIDER_HELPER_PATH", rule)
        printf_lines = [line for line in rule.splitlines() if line.strip().startswith("printf")]
        self.assertEqual(len(printf_lines), 1)
        command = printf_lines[0]
        for forbidden in ("install.sh", "apt-get", "systemctl", "usermod", "--providers-only", "ALL=(ALL)", "ALL = (ALL)"):
            self.assertNotIn(forbidden, command)
        # No wildcard suffix: the entry is the bare helper path (the helper
        # validates action + arguments itself).
        self.assertNotIn(" *", command)

    def test_provider_helper_rejects_unknown_actions_and_packages(self):
        helper = PROVIDER_HELPER.read_text()
        # Fail-closed dispatch: unknown actions exit 2, never fall through
        # to a shell or generic command runner.
        self.assertIn('*)', helper)
        self.assertIn("Unknown action", helper)
        self.assertNotIn("eval", helper)
        self.assertNotIn("bash -c", helper)
        # Package installs are allow-listed per manager on both sides.
        self.assertIn("allowlisted_package", helper)
        for package in ("libnss-mdns", "avahi-daemon", "pipewire-alsa"):
            self.assertIn(package, helper)
        # Firewall rule ids are fixed; purpose labels are charset-bounded.
        self.assertIn("mdns_5353_udp", helper)
        self.assertIn("spotifyd_zeroconf_4444_tcp", helper)
        # Spotify apt repo pins URL + fingerprint inside the helper.
        self.assertIn("repository.spotify.com", helper)
        self.assertIn("E1096BCBFF6D418796DE78515384CE82BA52C83A", helper)
        # SUDO_USER-derived identity only; never a username argument.
        self.assertIn("SUDO_USER", helper)

    def test_provider_helper_actions_are_fixed_and_bounded(self):
        helper = PROVIDER_HELPER.read_text()
        for action in ("packages", "backports-pipewire-alsa", "avahi-enable",
                       "journal-group", "fw-open", "fw-query",
                       "spotify-apt-repo", "state-mirror"):
            self.assertIn(f"{action})", helper)
        # Every privileged step validates its argument count strictly.
        self.assertGreaterEqual(helper.count("takes no arguments"), 3)
        self.assertIn("takes no arguments", helper)

    def test_provider_privilege_state_is_recorded_for_uninstall(self):
        self.assertIn("privilege_escalation", self.install)
        self.assertIn("sudoers_sha256", self.install)
        self.assertIn("providers.privilege_escalation.installed_by_fxroute", self.install)
        self.assertIn("remove_provider_privilege_escalation", self.uninstall)
        self.assertIn("fxroute-provider-privileged", self.uninstall)

    def test_full_install_sets_up_the_provider_helper(self):
        # Fresh images must be UI-ready without a manual bootstrap: the
        # full installer installs the helper right after the CIFS helper.
        body = self.install[self.install.index("\nmain() {"):]
        self.assertIn("install_provider_privileged_helper", body)
        self.assertLess(
            body.index("install_network_library_helper"),
            body.index("install_provider_privileged_helper"),
        )
        self.assertLess(
            body.index("install_provider_privileged_helper"),
            body.index("setup_python_env"),
        )

    def test_provider_install_endpoint_runs_unprivileged(self):
        streaming_text = (ROOT / "streaming" / "api.py").read_text()
        body = streaming_text.split("async def _run_provider_installer_op")[1]
        body = body.split("\n@router.")[0]
        self.assertNotIn('"sudo", "-n"', body)
        # The 503 mapping keys on the contract marker + machine detail only;
        # the trailing prose hint is free-form.
        self.assertIn("provider_contract.MARKER_HELPER_MISSING", body)
        self.assertIn("_PROVIDER_HELPER_MISSING_CONTRACT", body)
        self.assertIn("X-FXRoute-Provider-Contract", body)
        self.assertIn("503", body)

    def test_provider_helper_missing_detail_is_the_contract_pair(self):
        expected = (
            f"{provider_contract.DETAILS_KEY_HELPER_MISSING}="
            f"{provider_contract.DETAILS_VALUE_HELPER_MISSING}"
            f";{provider_contract.DETAILS_KEY_RERUN_INSTALL}="
            f"{provider_contract.DETAILS_VALUE_RERUN_INSTALL}"
        )
        streaming_text = (ROOT / "streaming" / "api.py").read_text()
        self.assertIn("_PROVIDER_HELPER_MISSING_CONTRACT = (", streaming_text)
        # The source must build the contract value from the constants (no
        # raw duplicated literals), and the constants must equal the pinned
        # machine values below.
        block = streaming_text[streaming_text.index("_PROVIDER_HELPER_MISSING_CONTRACT = ("):]
        block = block[:block.index(")")]
        for name in (
            "DETAILS_KEY_HELPER_MISSING",
            "DETAILS_VALUE_HELPER_MISSING",
            "DETAILS_KEY_RERUN_INSTALL",
            "DETAILS_VALUE_RERUN_INSTALL",
        ):
            self.assertIn(f"provider_contract.{name}", block)
        self.assertEqual(provider_contract.DETAILS_KEY_HELPER_MISSING, "reason")
        self.assertEqual(provider_contract.DETAILS_VALUE_HELPER_MISSING, "helper-missing")
        self.assertEqual(provider_contract.DETAILS_KEY_RERUN_INSTALL, "rerun")
        self.assertEqual(provider_contract.DETAILS_VALUE_RERUN_INSTALL, "rerun-full-install")

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
        # The Settings id 'spotify' removes spotify_desktop AND spotifyd
        # components, so all three state sections must be cleared together;
        # leaving 'spotifyd' records behind made the next Spotify install
        # refuse to reinstall (order-dependent provider installs).
        self.assertIn('"spotify": ["spotify", "spotify_desktop", "spotifyd"]', body)
        self.assertIn('for section in sections:', body)
        self.assertIn('providers.pop(section, None)', body)


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
        cls.streaming_api_text = (ROOT / "streaming" / "api.py").read_text()

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
        body = self.streaming_api_text.split("async def api_streaming_provider_install")[1]
        body = body.split("\n@router.")[0]
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
        cls.streaming_api_text = (ROOT / "streaming" / "api.py").read_text()

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
            self.assertIn(token, self.streaming_api_text)


class ProviderReinstallActivationTests(unittest.TestCase):
    """Uninstall -> reinstall must reactivate the provider like a first install.

    A Settings uninstall persists enabled=false for the provider
    (runProviderUninstall). The shared install flow must therefore turn the
    provider back on after a successful reinstall, exactly like the absent
    activation entry that already means "enabled" for a first install. The
    flow is provider-agnostic, so the single fix must cover spotify, qobuz
    and tidal alike.
    """

    PROVIDERS = ("spotify", "qobuz", "tidal")

    @classmethod
    def setUpClass(cls):
        cls.app_js = (ROOT / "static" / "app.js").read_text()
        cls.streaming_api_text = (ROOT / "streaming" / "api.py").read_text()

    @classmethod
    def _function_body(cls, name):
        start = cls.app_js.index(f"async function {name}(")
        rest = cls.app_js[start:]
        for marker in re.finditer(r"^(async )?function \w+\(", rest, re.MULTILINE):
            if marker.start() == 0:
                continue
            return rest[:marker.start()]
        return rest

    def test_uninstall_flow_disables_the_provider(self):
        body = self._function_body("runProviderUninstall")
        self.assertIn("await setProviderEnabled(providerId, false);", body)

    def test_install_flow_reenables_a_disabled_provider(self):
        body = self._function_body("runProviderInstall")
        # The re-enable must follow a reported success (resp.ok) and precede
        # the error branch: failed installs never flip the activation state.
        self.assertIn("setProviderEnabled(providerId, true)", body)
        self.assertLess(
            body.index("if (!resp.ok) throw new Error"),
            body.index("setProviderEnabled(providerId, true)"),
        )
        self.assertLess(
            body.index("setProviderEnabled(providerId, true)"),
            body.index("} catch (error) {"),
        )

    def test_reenable_acts_only_on_a_disabled_provider(self):
        body = self._function_body("runProviderInstall")
        # Mirror of the TIDAL login click: enable only when the uninstall flow
        # actually left the provider disabled (first installs already default
        # to enabled and must not round-trip an extra write).
        self.assertIn(
            "if (provider && provider.enabled === false) await setProviderEnabled(providerId, true);",
            body,
        )

    def test_every_ui_provider_rides_the_shared_install_uninstall_flow(self):
        # Both action buttons render from the same per-provider row template
        # and are wired to the shared handlers, so the activation fix applies
        # to every provider the UI can (re)install.
        self.assertIn('data-provider-install="${provider.id}"', self.app_js)
        self.assertIn('data-provider-uninstall="${provider.id}"', self.app_js)
        self.assertIn(
            "runProviderInstall(button.getAttribute('data-provider-install'))",
            self.app_js,
        )
        self.assertIn(
            "runProviderUninstall(button.getAttribute('data-provider-uninstall'))",
            self.app_js,
        )
        self.assertIn('fetch(`/api/streaming/providers/${encodeURIComponent(providerId)}/install`', self.app_js)
        self.assertIn('fetch(`/api/streaming/providers/${encodeURIComponent(providerId)}/uninstall`', self.app_js)

    def test_backend_ui_install_and_uninstall_cover_all_three_providers(self):
        body = self.streaming_api_text.split("async def api_streaming_provider_install")[1]
        body = body.split('\n@router.post("/api/streaming/providers/{provider_id}/uninstall')[0]
        for provider in self.PROVIDERS:
            self.assertIn(f'"{provider}"', body)
        body = self.streaming_api_text.split("async def api_streaming_provider_uninstall")[1]
        body = body.split("\n@router.post(")[0]
        self.assertIn('if provider_id not in {"spotify", "qobuz", "tidal"}:', body)


if __name__ == "__main__":
    unittest.main()
