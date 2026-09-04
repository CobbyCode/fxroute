#!/usr/bin/env python3
"""Behavior tests for the headless Armbian Wi-Fi setup service."""

import importlib.util
import http.client
import json
import re
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WEB_CONFIG = ROOT / "armbian" / "armbian-web-config.py"
VALID_SSH_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAICxEedFc5/SXgmFnMOYyGoi1DN2at6LTbBysqqSOOgN7 "
    "test"
)


def load_web_config():
    spec = importlib.util.spec_from_file_location("armbian_web_config", WEB_CONFIG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArmbianWebConfigBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web = load_web_config()

    def test_wired_carrier_suppresses_onboarding_even_with_wifi_present(self):
        with tempfile.TemporaryDirectory() as directory:
            net_root = Path(directory)
            (net_root / "eth0").mkdir()
            (net_root / "wlan0").mkdir()
            (net_root / "eth0" / "carrier").write_text("1", encoding="ascii")
            (net_root / "wlan0" / "carrier").write_text("0", encoding="ascii")

            self.assertTrue(self.web.has_ethernet_carrier(str(net_root)))

            (net_root / "eth0" / "carrier").write_text("0", encoding="ascii")
            self.assertFalse(self.web.has_ethernet_carrier(str(net_root)))

    def test_ethernet_is_usable_only_after_a_global_address_is_present(self):
        with tempfile.TemporaryDirectory() as directory:
            net_root = Path(directory)
            (net_root / "eth0").mkdir()
            (net_root / "eth0" / "carrier").write_text("1", encoding="ascii")
            address = self.web.subprocess.CompletedProcess(
                ["ip"], 0, "2: eth0    inet 192.0.2.2/24 scope global\n", ""
            )
            with mock.patch.object(self.web, "command", return_value=address):
                self.assertTrue(self.web.has_usable_ethernet(str(net_root)))

            no_address = self.web.subprocess.CompletedProcess(["ip"], 0, "", "")
            with mock.patch.object(self.web, "command", return_value=no_address):
                self.assertFalse(self.web.has_usable_ethernet(str(net_root)))

    def test_ethernet_carrier_waits_for_dhcp_before_falling_back_to_ap(self):
        onboarding = self.web.Onboarding("wlan0", "rpi4b")
        with (
            mock.patch.object(self.web, "has_usable_ethernet", side_effect=[False, True]),
            mock.patch.object(self.web, "has_ethernet_carrier", return_value=True),
            mock.patch.object(self.web.time, "sleep"),
        ):
            self.assertTrue(onboarding.wait_for_ethernet(timeout=1))

    def test_wifi_netplan_keeps_networkd_and_gives_wired_routes_priority(self):
        config = self.web.build_wifi_netplan("wlan0", "Cafe:5G", "correct horse", "DE")

        self.assertIn("renderer: networkd", config)
        self.assertIn('"Cafe:5G":', config)
        self.assertIn('password: "correct horse"', config)
        self.assertIn("route-metric: 600", config)
        self.assertIn("regulatory-domain: DE", config)

    def test_wifi_validation_rejects_unsafe_or_unusable_values(self):
        with self.assertRaises(ValueError):
            self.web.validate_setup("", "", "GB")
        with self.assertRaises(ValueError):
            self.web.validate_setup("network", "short", "GB")
        with self.assertRaises(ValueError):
            self.web.validate_setup("network", "long enough", "Germany")
        with self.assertRaises(ValueError):
            self.web.validate_setup("network", "long enough", "ZZ")

        self.assertEqual(
            self.web.validate_setup("network", "long enough", "de"),
            ("network", "long enough", "DE"),
        )

    def test_account_validation_requires_a_real_user_and_short_password_only(self):
        with self.assertRaises(ValueError):
            self.web.validate_account(
                "", "long enough password", "long enough password", "ssh-ed25519 AAAA"
            )
        with self.assertRaises(ValueError):
            self.web.validate_account(
                "root", "long enough password", "long enough password", "ssh-ed25519 AAAA"
            )
        with self.assertRaises(ValueError):
            self.web.validate_account("operator", "123", "123", "")
        with self.assertRaises(ValueError):
            self.web.validate_account("operator", "😀", "😀", "")
        with self.assertRaises(ValueError):
            self.web.validate_account(
                "operator", "long enough password", "different password", ""
            )

        self.assertEqual(
            self.web.validate_account("Operator", "four", "four", ""),
            ("operator", "four", ""),
        )
        self.assertEqual(
            self.web.validate_account("Operator", "four:", "four:", ""),
            ("operator", "four:", ""),
        )

        with self.assertRaises(ValueError):
            self.web.validate_account(
                "operator", "long enough password", "long enough password", "not-a-key"
            )

        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "test-key"
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
                check=True,
            )
            ssh_key = key_path.with_name("test-key.pub").read_text(encoding="utf-8").strip()

            self.assertEqual(
                self.web.validate_account(
                    "Operator",
                    "long enough password",
                    "long enough password",
                    ssh_key,
                ),
                ("operator", "long enough password", ssh_key),
            )

    def test_account_creation_sets_password_and_authorized_key_without_persisting_password(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "operator"
            home.mkdir()
            record = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir=str(home))
            calls = []

            def fake_command(args, check=True, input_text=None):
                calls.append((args, check, input_text))
                return self.web.subprocess.CompletedProcess(args, 0, "", "")

            with (
                mock.patch.object(self.web.pwd, "getpwnam", return_value=record),
                mock.patch.object(self.web, "command", side_effect=fake_command),
                mock.patch.object(self.web.os, "chown"),
            ):
                self.web.create_account(
                    "operator", "long enough password", VALID_SSH_KEY, allow_existing=True
                )

            authorized_keys = home / ".ssh" / "authorized_keys"
            self.assertEqual(
                authorized_keys.read_text(encoding="utf-8"),
                f"{VALID_SSH_KEY}\n",
            )
            self.assertIn(
                (["chpasswd"], True, "operator:long enough password\n"), calls
            )

    def test_account_creation_without_ssh_key_does_not_create_ssh_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "operator"
            home.mkdir()
            record = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir=str(home))

            def fake_command(args, check=True, input_text=None):
                return self.web.subprocess.CompletedProcess(args, 0, "", "")

            with (
                mock.patch.object(self.web.pwd, "getpwnam", return_value=record),
                mock.patch.object(self.web, "command", side_effect=fake_command),
                mock.patch.object(self.web.os, "chown"),
            ):
                self.web.create_account(
                    "operator", "four", "", allow_existing=True
                )

            self.assertFalse((home / ".ssh").exists())

    def test_new_account_claim_is_recorded_before_user_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            account_file = Path(directory) / "account"
            onboarding = self.web.Onboarding("", "rpi4b")

            def create_account(*_args, **_kwargs):
                self.assertEqual(account_file.read_text(encoding="ascii"), "operator\n")
                return True

            with (
                mock.patch.object(self.web, "ACCOUNT_FILE", account_file),
                mock.patch.object(self.web.pwd, "getpwnam", side_effect=KeyError),
                mock.patch.object(self.web, "create_account", side_effect=create_account),
                mock.patch.object(self.web, "configure_ssh_access"),
            ):
                success, message = onboarding.configure_account(
                    "operator", "long enough password", VALID_SSH_KEY
                )

            self.assertTrue(success)
            self.assertEqual(message, "Account configured")

    def test_ethernet_submit_creates_account_metadata_and_completes_onboarding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account_file = root / "account"
            configured_marker = root / "configured"
            onboarding = self.web.Onboarding("", "rpi4b")

            with (
                mock.patch.object(self.web, "ACCOUNT_FILE", account_file),
                mock.patch.object(self.web, "CONFIGURED_MARKER", configured_marker),
                mock.patch.object(self.web, "has_usable_ethernet", return_value=True),
                mock.patch.object(self.web, "create_account"),
                mock.patch.object(self.web, "configure_ssh_access"),
            ):
                success, message = onboarding.submit(
                    "Operator",
                    "long enough password",
                    "long enough password",
                    VALID_SSH_KEY,
                    "",
                    "",
                    "GB",
                )

            self.assertTrue(success)
            self.assertEqual(message, "Setup complete")
            self.assertEqual(account_file.read_text(encoding="utf-8"), "operator\n")
            self.assertTrue(configured_marker.exists())

    def test_ethernet_disconnect_before_completion_keeps_onboarding_active(self):
        onboarding = self.web.Onboarding("wlan0", "rpi4b")
        with (
            mock.patch.object(self.web, "has_usable_ethernet", side_effect=[True, False]),
            mock.patch.object(
                onboarding, "configure_account", return_value=(True, "Account configured")
            ),
            mock.patch.object(onboarding, "mark_configured") as mark_configured,
        ):
            success, message = onboarding.submit(
                "operator",
                "long enough password",
                "long enough password",
                VALID_SSH_KEY,
                "",
                "",
                "GB",
            )

        self.assertFalse(success)
        self.assertEqual(message, "Wired Ethernet is no longer available; provide Wi-Fi")
        mark_configured.assert_not_called()

    def test_setup_page_contains_the_clean_account_and_network_form(self):
        page = self.web.setup_page("rpi4b", preview=True)

        self.assertIn(
            "Configure your FXRoute administrator account and network.", page
        )
        self.assertIn('minlength="4"', page)
        self.assertNotIn('minlength="12"', page)
        self.assertIn('<details class="advanced">', page)
        self.assertIn("Advanced / SSH", page)
        self.assertNotIn('name="ssh_key" required', page)
        self.assertIn('id="wifi_country" name="wifi_country"', page)
        self.assertIn("Germany (DE)", page)
        self.assertIn('id="wifi-password-note"', page)
        self.assertIn("wifiPassword.required = selectedNetworkSecured === true;", page)
        self.assertIn('wifiPassword.value = "";', page)
        self.assertIn("selectedNetworkSecured = null;", page)
        self.assertIn("Rescan", page)
        self.assertIn("Scan again", page)
        self.assertIn("/api/wifi/scan", page)
        self.assertIn('<meta name="description"', page)
        self.assertIn("Enter network manually", page)
        self.assertIn('role="listitem"', page)
        self.assertIn('item.setAttribute("role", "listitem");', page)
        self.assertIn("selectedNetworkFound", page)
        self.assertIn("clearMissingNetworkSelection();", page)
        self.assertIn(
            "if (selectedSsid && !selectedNetworkFound) clearMissingNetworkSelection();\n    syncNetworkFields();",
            page,
        )
        self.assertIn(
            'manualToggle.textContent = opening ? "Hide manual entry" : "Enter network manually";',
            page,
        )
        self.assertIn('aria-controls="manual-network"', page)
        self.assertIn("if (ssidInput.readOnly) {", page)
        self.assertIn('else if (!ssidInput.value) selection.textContent = "No network selected";', page)
        self.assertIn(".field-grid > .field { margin-top: 0; }", page)
        self.assertIn(
            'networkList.appendChild(stateMessage("Scan unavailable. Try again or enter the name manually.", true));',
            page,
        )
        self.assertIn(
            'const previousNetworkItems = [...networkList.querySelectorAll(".network-item")];',
            page,
        )
        self.assertIn("networkList.replaceChildren(...previousNetworkItems);", page)
        self.assertRegex(page, r"\.advanced summary \{[^}]*min-height: 44px;")
        self.assertIn(".advanced summary:focus-visible {", page)
        self.assertIn('rel="icon"', page)
        self.assertIn("min-height: 44px", page)
        self.assertIn("font-family: inherit;", page)
        self.assertNotIn("font: 700 .9rem/1.2 inherit", page)
        self.assertIn("0 0 0 6px var(--teal)", page)
        self.assertIn("textarea::placeholder { color: var(--muted); opacity: 1; }", page)
        for technical_hint in (
            "10.42.0.1",
            "armbiansetup",
            "temporary network",
            "Ethernet is preferred",
        ):
            self.assertNotIn(technical_hint, page)

    def test_setup_ssid_uses_the_runtime_hostname(self):
        self.assertEqual(self.web.onboarding_ssid("rpi5b"), "rpi5b-armbiansetup")
        self.assertNotEqual(self.web.onboarding_ssid("rpi5b"), "rpi4b-armbiansetup")

    def test_access_point_is_open_and_uses_runtime_hostname(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            hostapd_path = run_dir / "hostapd.conf"
            dnsmasq_path = run_dir / "dnsmasq.conf"
            process = mock.Mock()
            process.poll.return_value = None
            onboarding = self.web.Onboarding("wlan0", "rpi5b")

            with (
                mock.patch.object(self.web, "RUN_DIR", run_dir),
                mock.patch.object(self.web, "HOSTAPD_CONFIG", hostapd_path),
                mock.patch.object(self.web, "DNSMASQ_CONFIG", dnsmasq_path),
                mock.patch.object(
                    self.web.subprocess, "Popen", side_effect=[process, process]
                ),
                mock.patch.object(self.web, "command"),
                mock.patch.object(self.web.time, "sleep"),
            ):
                onboarding.start_access_point()
                onboarding.stop_access_point()

            hostapd = hostapd_path.read_text(encoding="ascii")
            self.assertIn("ssid=rpi5b-armbiansetup", hostapd)
            self.assertNotIn("wpa_psk", hostapd)
            self.assertNotIn("wpa_passphrase", hostapd)

    def test_corrupt_tls_pair_is_regenerated(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            cert_path = run_dir / "setup.crt"
            key_path = run_dir / "setup.key"
            cert_path.write_text("corrupt\n", encoding="ascii")
            key_path.write_text("corrupt\n", encoding="ascii")

            with (
                mock.patch.object(self.web, "RUN_DIR", run_dir),
                mock.patch.object(self.web, "TLS_CERT_PATH", cert_path),
                mock.patch.object(self.web, "TLS_KEY_PATH", key_path),
            ):
                self.web.ensure_tls_certificate("rpi4b")
                self.assertTrue(
                    self.web.certificate_pair_is_usable(cert_path, key_path)
                )
                key_path.write_text("mismatched\n", encoding="ascii")
                self.web.ensure_tls_certificate("rpi4b")
                self.assertTrue(
                    self.web.certificate_pair_is_usable(cert_path, key_path)
                )

    def test_dead_access_point_process_is_restarted(self):
        onboarding = self.web.Onboarding("wlan0", "rpi4b")
        exited = mock.Mock()
        exited.poll.return_value = 1
        running = mock.Mock()
        running.poll.return_value = None
        onboarding.ap_active = True
        onboarding.ap_processes = [exited, running]

        with (
            mock.patch.object(
                onboarding.shutdown_event, "wait", side_effect=[False, True]
            ),
            mock.patch.object(self.web, "has_usable_ethernet", return_value=False),
            mock.patch.object(onboarding, "stop_access_point") as stop_access_point,
            mock.patch.object(onboarding, "start_access_point") as start_access_point,
        ):
            onboarding.monitor_ethernet()

        stop_access_point.assert_called_once_with()
        start_access_point.assert_called_once_with()

    def test_successful_wifi_configuration_does_not_flush_the_station(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            netplan_path = root / "30-wifis-dhcp.yaml"
            configured_marker = root / "configured"
            onboarding = self.web.Onboarding("wlan0", "rpi4b")
            onboarding.wait_for_network = lambda timeout=60: True

            with (
                mock.patch.object(self.web, "NETPLAN_PATH", netplan_path),
                mock.patch.object(self.web, "CONFIGURED_MARKER", configured_marker),
                mock.patch.object(self.web, "has_usable_ethernet", return_value=False),
                mock.patch.object(self.web, "command") as command,
            ):
                success, _message = onboarding.configure_wifi(
                    "network", "long enough", "GB"
                )
                self.assertTrue(success)
                onboarding.stop_access_point()

            self.assertTrue(netplan_path.exists())
            self.assertFalse(configured_marker.exists())
            calls = [call.args[0] for call in command.call_args_list]
            self.assertFalse(any(args[0:3] == ["ip", "addr", "flush"] for args in calls))
            self.assertIn(["netplan", "apply"], calls)
            self.assertNotIn(["netplan", "apply", "--timeout", "0"], calls)

    def test_interrupted_wifi_setup_waits_for_connection_before_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account_file = root / "account"
            netplan_path = root / "30-wifis-dhcp.yaml"
            configured_marker = root / "configured"
            account_file.write_text("operator\n", encoding="ascii")
            netplan_path.write_text("network:\n", encoding="ascii")
            onboarding = self.web.Onboarding("wlan0", "rpi4b")
            onboarding.wait_for_network = lambda timeout=60: True

            with (
                mock.patch.object(self.web, "ACCOUNT_FILE", account_file),
                mock.patch.object(self.web, "NETPLAN_PATH", netplan_path),
                mock.patch.object(self.web, "has_usable_ethernet", return_value=False),
                mock.patch.object(onboarding, "mark_configured") as mark_configured,
            ):
                self.assertTrue(onboarding.recover_interrupted_setup())

            self.assertTrue(netplan_path.exists())
            mark_configured.assert_called_once_with()

    def test_wired_carrier_during_wifi_submission_does_not_mark_bad_wifi_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured_marker = root / "configured"
            onboarding = self.web.Onboarding("wlan0", "rpi4b")

            with (
                mock.patch.object(self.web, "CONFIGURED_MARKER", configured_marker),
                mock.patch.object(self.web, "has_usable_ethernet", return_value=True),
            ):
                success, message = onboarding.configure_wifi(
                    "wrong-network", "long enough", "GB"
                )

            self.assertFalse(success)
            self.assertEqual(message, "Wired Ethernet detected")
            self.assertFalse(configured_marker.exists())

    def test_ethernet_onboarding_serves_http_without_starting_the_access_point(self):
        self.assertFalse(self.web.should_start_access_point(True, "wlan0"))
        self.assertTrue(self.web.should_start_access_point(False, "wlan0"))
        self.assertFalse(self.web.should_start_access_point(False, ""))

        page = self.web.setup_page("rpi4b", ethernet=True, ap_active=False)
        self.assertIn('action="/setup"', page)
        self.assertNotIn("Ethernet", page)

    def test_ap_setup_page_posts_account_credentials_over_tls(self):
        page = self.web.setup_page("rpi4b", ethernet=False, ap_active=True)
        self.assertIn('action="https://10.42.0.1/setup"', page)
        self.assertIn('data-preview="false"', page)
        self.assertNotIn('window.location.host + "/setup"', page)
        self.assertNotIn('name="setup_password"', page)

    def test_setup_page_renders_a_clean_desktop_password_country_layout(self):
        # Rendered-output contract: the embedded <style> must be valid CSS and
        # the form markup balanced, so the desktop password/country split
        # actually applies. A stray "#" comment inside the CSS template once
        # corrupted the selector prelude and silently dropped the whole rule
        # (CSS error recovery discards it); the f-string braces must also
        # never leak into the rendered page.
        page = self.web.setup_page("rpi4b", preview=True)

        self.assertNotIn("{{", page)
        self.assertNotIn("}}", page)

        # The two-field grid container exists exactly once and directly
        # follows the (closed) selection hint paragraph.
        self.assertEqual(
            page.count('<div class="field-grid fields-password-country">'), 1
        )
        self.assertRegex(
            page,
            r'<p id="wifi-selection" class="selection">No network selected</p>'
            r'\s*<div class="field-grid fields-password-country">',
        )
        self.assertEqual(
            len(re.findall(r"<div\b", page)), len(re.findall(r"</div>", page))
        )

        style = page[page.index("<style>") + len("<style>") : page.index("</style>")]

        def css_preludes(declaration_regex):
            # Selector preludes of rules containing the declaration, with
            # comments stripped and whitespace collapsed. This mirrors how a
            # browser tokenizes the stylesheet before selector matching.
            preludes = []
            for match in re.finditer(
                r"([^{}]+)\{\s*" + declaration_regex + r"\s*;", style
            ):
                text = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
                preludes.append(re.sub(r"\s+", " ", text).strip())
            return preludes

        self.assertIn(
            ".field-grid.fields-password-country",
            css_preludes(r"grid-template-columns:\s*minmax\(0,\s*1fr\)\s*150px"),
        )
        # The explicit narrow-screen override keeps the fields stacked.
        self.assertIn(
            ".field-grid.fields-password-country",
            css_preludes(r"grid-template-columns:\s*1fr"),
        )

        # Class defect: no rule may carry a corrupted selector prelude
        # (newlines or non-CSS "#" comments). Browsers drop the entire rule
        # in that case, silently undoing a layout.
        for match in re.finditer(r"([^{}]+)\{", style):
            prelude = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
            prelude = re.sub(r"\s+", " ", prelude).strip()
            if not prelude:
                continue
            self.assertNotIn("\n", prelude, f"corrupted CSS prelude: {prelude!r}")
            self.assertIsNone(
                re.search(r"#(\s|$)", prelude),
                f"non-CSS comment marker in CSS prelude: {prelude!r}",
            )
            self.assertRegex(prelude, r"^[@.:*&a-zA-Z0-9\[]")

    def test_country_dropdown_keeps_the_existing_default_and_has_a_german_label(self):
        page = self.web.setup_page("rpi4b", preview=True)

        self.assertIn(
            '<option value="GB" selected>United Kingdom (GB)</option>', page
        )
        self.assertIn('<option value="DE">Germany (DE)</option>', page)
        self.assertIn('<option value="KE">Kenya (KE)</option>', page)
        self.assertIn('<option value="RU">Russia (RU)</option>', page)
        self.assertGreaterEqual(page.count("<option value="), 200)

    def test_wifi_networks_use_cached_results_when_ap_scanning_fails(self):
        onboarding = self.web.Onboarding("wlan0", "rpi4b")
        cached = [{"ssid": "Home", "signal": -50, "secured": True}]
        onboarding.wifi_scan_cache = cached

        with mock.patch.object(
            self.web, "scan_wifi_networks", side_effect=RuntimeError("busy")
        ):
            self.assertEqual(onboarding.wifi_networks(), cached)

    def test_prime_wifi_scan_brings_the_station_interface_up(self):
        onboarding = self.web.Onboarding("wlan0", "rpi4b")
        with (
            mock.patch.object(self.web, "command") as command,
            mock.patch.object(onboarding, "wifi_networks") as wifi_networks,
        ):
            onboarding.prime_wifi_scan()

        command.assert_called_once_with(
            ["ip", "link", "set", "dev", "wlan0", "up"], check=False
        )
        wifi_networks.assert_called_once_with()

    def test_wifi_networks_keep_the_cache_when_an_active_ap_scan_returns_empty(self):
        onboarding = self.web.Onboarding("wlan0", "rpi4b")
        onboarding.ap_active = True
        cached = [{"ssid": "Home", "signal": -50, "secured": True}]
        onboarding.wifi_scan_cache = cached

        with mock.patch.object(self.web, "scan_wifi_networks", return_value=[]):
            self.assertEqual(onboarding.wifi_networks(), cached)

    def test_wifi_scan_uses_a_timeout_for_unresponsive_drivers(self):
        timeout = subprocess.TimeoutExpired(
            ["iw", "dev", "wlan0", "scan"], self.web.WIFI_SCAN_TIMEOUT
        )
        with mock.patch.object(self.web, "command", side_effect=timeout) as command:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                self.web.scan_wifi_networks("wlan0")

        command.assert_called_once_with(
            ["iw", "dev", "wlan0", "scan"],
            check=False,
            timeout=self.web.WIFI_SCAN_TIMEOUT,
        )

    def test_busy_wifi_scan_uses_existing_cache_without_starting_another_scan(self):
        onboarding = self.web.Onboarding("wlan0", "rpi4b")
        cached = [{"ssid": "Home", "signal": -50, "secured": True}]
        onboarding.wifi_scan_cache = cached
        onboarding.wifi_scan_lock.acquire()
        try:
            with mock.patch.object(self.web, "scan_wifi_networks") as scan:
                self.assertEqual(onboarding.wifi_networks(), cached)
            scan.assert_not_called()
        finally:
            onboarding.wifi_scan_lock.release()

    def test_wifi_networks_hide_the_temporary_setup_access_point(self):
        onboarding = self.web.Onboarding("wlan0", "rpi4b")
        networks = [
            {"ssid": "rpi4b-armbiansetup", "signal": -30, "secured": False},
            {"ssid": "Home", "signal": -50, "secured": True},
        ]

        with mock.patch.object(self.web, "scan_wifi_networks", return_value=networks):
            self.assertEqual(onboarding.wifi_networks(), [networks[1]])

    def test_preview_submit_ignores_a_host_configured_marker(self):
        onboarding = self.web.Onboarding("preview-wlan0", "preview", preview=True)
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "configured"
            marker.touch()
            with mock.patch.object(self.web, "CONFIGURED_MARKER", marker):
                success, message = onboarding.submit(
                    "operator", "four", "four", "", "", "", "DE"
                )

        self.assertTrue(success)
        self.assertEqual(message, "Preview submission accepted")

    def test_wifi_scan_parser_returns_visible_networks_sorted_by_signal(self):
        scan = """
BSS 11:22:33:44:55:66(on wlan0)
        signal: -62.00 dBm
        SSID: Open cafe
BSS aa:bb:cc:dd:ee:ff(on wlan0)
        SSID: Studio Wi-Fi
        signal: -48.00 dBm
        RSN:
BSS 22:33:44:55:66:77(on wlan0)
        SSID: Studio Wi-Fi
        signal: -38.00 dBm
        RSN:
BSS 33:44:55:66:77:88(on wlan0)
        SSID:
        signal: -20.00 dBm
"""

        self.assertEqual(
            self.web.parse_wifi_scan(scan),
            [
                {"ssid": "Studio Wi-Fi", "signal": -38, "secured": True},
                {"ssid": "Open cafe", "signal": -62, "secured": False},
            ],
        )

    def test_wifi_scan_parser_decodes_escaped_and_preserves_ssid_spaces(self):
        scan = r"""
BSS 11:22:33:44:55:66(on wlan0)
        signal: -42.00 dBm
        SSID: \x20Studio\x20
"""

        self.assertEqual(
            self.web.parse_wifi_scan(scan),
            [{"ssid": " Studio ", "signal": -42, "secured": False}],
        )

    def test_wifi_scan_parser_does_not_treat_bss_load_as_a_new_network(self):
        scan = """
BSS 11:22:33:44:55:66(on wlan0)
        signal: -42.00 dBm
        SSID: Studio
BSS Load:
                station count: 1
        RSN:
"""

        self.assertEqual(
            self.web.parse_wifi_scan(scan),
            [{"ssid": "Studio", "signal": -42, "secured": True}],
        )

    def test_wifi_scan_parser_exposes_the_bss_regulatory_country(self):
        scan = """
BSS 11:22:33:44:55:66(on wlan0)
        signal: -42.00 dBm
        SSID: Studio Wi-Fi
        RSN:
        Country: DE\tenvironment Indoor
BSS aa:bb:cc:dd:ee:ff(on wlan0)
        signal: -50.00 dBm
        SSID: Open cafe
        Country: AT\tenvironment Outdoors
"""

        self.assertEqual(
            self.web.parse_wifi_scan(scan),
            [
                {
                    "ssid": "Studio Wi-Fi",
                    "signal": -42,
                    "secured": True,
                    "country": "DE",
                },
                {
                    "ssid": "Open cafe",
                    "signal": -50,
                    "secured": False,
                    "country": "AT",
                },
            ],
        )

    def test_wifi_scan_parser_omits_the_country_when_the_ap_reports_none(self):
        scan = """
BSS 11:22:33:44:55:66(on wlan0)
        signal: -42.00 dBm
        SSID: No country
        RSN:
        Country:\n"""

        self.assertEqual(
            self.web.parse_wifi_scan(scan),
            [{"ssid": "No country", "signal": -42, "secured": True}],
        )

    def test_wifi_scan_parser_keeps_the_strongest_bss_country_for_duplicate_ssids(self):
        scan = """
BSS 11:22:33:44:55:66(on wlan0)
        signal: -38.00 dBm
        SSID: Studio Wi-Fi
        RSN:
        Country: DE\tenvironment Indoor
BSS aa:bb:cc:dd:ee:ff(on wlan0)
        signal: -48.00 dBm
        SSID: Studio Wi-Fi
        Country: GB\tenvironment Indoor
"""

        self.assertEqual(
            self.web.parse_wifi_scan(scan),
            [
                {
                    "ssid": "Studio Wi-Fi",
                    "signal": -38,
                    "secured": True,
                    "country": "DE",
                }
            ],
        )

    def test_wifi_scan_parser_ignores_a_malformed_country_value(self):
        scan = """
BSS 11:22:33:44:55:66(on wlan0)
        signal: -42.00 dBm
        SSID: Studio
        RSN:
        Country: D1\tenvironment Indoor
"""

        self.assertEqual(
            self.web.parse_wifi_scan(scan),
            [{"ssid": "Studio", "signal": -42, "secured": True}],
        )

    def test_preview_onboarding_simulates_networks_without_system_changes(self):
        onboarding = self.web.Onboarding("preview-wlan0", "preview", preview=True)

        self.assertTrue(onboarding.preview)
        self.assertTrue(onboarding.has_usable_ethernet())
        self.assertEqual(onboarding.wifi_networks(), self.web.PREVIEW_WIFI_NETWORKS)
        with (
            mock.patch.object(self.web, "create_account") as create_account,
            mock.patch.object(self.web, "configure_ssh_access") as configure_ssh,
            mock.patch.object(self.web, "atomic_write") as atomic_write,
        ):
            success, message = onboarding.submit(
                "operator", "four", "four", "", "", "", "DE"
            )

        self.assertTrue(success)
        self.assertEqual(message, "Preview submission accepted")
        create_account.assert_not_called()
        configure_ssh.assert_not_called()
        atomic_write.assert_not_called()

    def test_wifi_scan_endpoint_returns_preview_networks(self):
        onboarding = self.web.Onboarding("preview-wlan0", "preview", preview=True)
        server = self.web.SetupServer(
            ("127.0.0.1", 0), self.web.SetupHandler, onboarding
        )
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/api/wifi/scan")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), self.web.PREVIEW_WIFI_NETWORKS)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(2)

    def test_existing_unselected_accounts_are_not_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            account_file = Path(directory) / "account"
            onboarding = self.web.Onboarding("", "rpi4b")
            record = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir="/home/operator")

            with (
                mock.patch.object(self.web, "ACCOUNT_FILE", account_file),
                mock.patch.object(self.web.pwd, "getpwnam", return_value=record),
                mock.patch.object(self.web, "create_account") as create_account,
            ):
                success, message = onboarding.configure_account(
                    "operator", "long enough password", VALID_SSH_KEY
                )

            self.assertFalse(success)
            self.assertIn("already exists", message)
            create_account.assert_not_called()

    def test_selected_account_can_be_corrected_on_a_network_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            account_file = Path(directory) / "account"
            account_file.write_text("operator\n", encoding="ascii")
            onboarding = self.web.Onboarding("", "rpi4b")
            record = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir="/home/operator")

            with (
                mock.patch.object(self.web, "ACCOUNT_FILE", account_file),
                mock.patch.object(self.web.pwd, "getpwnam", return_value=record),
                mock.patch.object(self.web, "create_account") as create_account,
                mock.patch.object(self.web, "configure_ssh_access"),
            ):
                success, message = onboarding.configure_account(
                    "operator", "another password", VALID_SSH_KEY
                )

            self.assertTrue(success)
            self.assertEqual(message, "Account configured")
            create_account.assert_called_once_with(
                "operator",
                "another password",
                VALID_SSH_KEY,
                allow_existing=True,
            )

    def test_ethernet_http_requests_are_redirected_to_tls(self):
        class FakeOnboarding:
            hostname = "rpi4b"
            ap_active = False

        server = self.web.SetupServer(
            ("127.0.0.1", 0), self.web.SetupHandler, FakeOnboarding()
        )
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            with mock.patch.object(self.web, "has_usable_ethernet", return_value=True):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request("GET", "/", headers={"Host": "attacker.invalid"})
                response = connection.getresponse()
                self.assertEqual(response.status, 302)
                self.assertEqual(response.getheader("Location"), "https://127.0.0.1/")
                response.read()
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(2)

    def test_ap_http_posts_are_redirected_to_tls_before_reading_credentials(self):
        class FakeOnboarding:
            hostname = "rpi4b"
            ap_active = True

        server = self.web.SetupServer(
            ("127.0.0.1", 0), self.web.SetupHandler, FakeOnboarding()
        )
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            with mock.patch.object(self.web, "has_usable_ethernet", return_value=False):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request("POST", "/setup", body=b"account_password=secret")
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                self.assertIsNone(response.getheader("Location"))
                response.read()
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(2)

    def test_completed_setup_rejects_late_submissions(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "configured"
            marker.touch()
            onboarding = self.web.Onboarding("", "rpi4b")

            with mock.patch.object(self.web, "CONFIGURED_MARKER", marker):
                success, message = onboarding.submit(
                    "operator",
                    "long enough password",
                    "long enough password",
                    VALID_SSH_KEY,
                    "",
                    "",
                    "GB",
                )

            self.assertFalse(success)
            self.assertEqual(message, "Setup has already been completed")

    def test_missing_wifi_interface_cannot_complete_headless_onboarding(self):
        onboarding = self.web.Onboarding("", "rpi4b")
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "not_logged_in_yet"
            marker.touch()
            with (
                mock.patch.object(self.web, "MARKER_PATH", marker),
                mock.patch.object(
                    self.web, "CONFIGURED_MARKER", marker.with_name("configured")
                ),
                mock.patch.object(self.web, "has_usable_ethernet", return_value=False),
                mock.patch.object(self.web, "has_ethernet_carrier", return_value=False),
            ):
                with self.assertRaises(RuntimeError):
                    onboarding.serve()

    def test_open_wifi_networks_emit_an_empty_access_point_mapping(self):
        config = self.web.build_wifi_netplan("wlan0", "open-network", "", "GB")

        self.assertIn('        "open-network": {}\n', config)

    def test_setup_acknowledgement_is_sent_before_network_teardown(self):
        configure_started = threading.Event()
        release_configuration = threading.Event()

        class FakeOnboarding:
            hostname = "rpi4b"
            ap_active = False

            def submit(
                self,
                _username,
                _account_password,
                _account_password_confirmation,
                _ssh_key,
                _wifi_ssid,
                _wifi_password,
                _country,
            ):
                configure_started.set()
                release_configuration.wait(2)
                return True, "Wi-Fi configured"

            def request_shutdown(self):
                server.shutdown()

        server = self.web.SetupServer(
            ("127.0.0.1", 0), self.web.SetupHandler, FakeOnboarding(), secure=True
        )
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            response_holder = {}

            def post_setup():
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=2
                )
                connection.request(
                    "POST",
                    "/setup",
                    body=(
                        b"username=operator&account_password=long%20enough%20password"
                        b"&account_password_confirm=long%20enough%20password"
                        b"&ssh_key=ssh-ed25519%20AAAAC3NzaC1lZDI1NTE5AAAAICxEedFc5%2FSXgmFnMOYyGoi1DN2at6LTbBysqqSOOgN7%20test"
                        b"&wifi_ssid=network&wifi_password=long%20enough&wifi_country=GB"
                    ),
                )
                response_holder["response"] = connection.getresponse()

            post_thread = threading.Thread(target=post_setup)
            with mock.patch.object(self.web, "has_usable_ethernet", return_value=False):
                post_thread.start()
                self.assertTrue(configure_started.wait(1))
                post_thread.join(1)
                self.assertEqual(response_holder["response"].status, 202)
                self.assertIn(
                    b"Applying FXRoute setup",
                    response_holder["response"].read1(1024),
                )
        finally:
            release_configuration.set()
            if "response" in response_holder:
                response_holder["response"].read()
                response_holder["response"].close()
            server.shutdown()
            server.server_close()
            server_thread.join(2)

    def test_failed_submission_reports_failure_after_the_in_progress_response(self):
        class FakeOnboarding:
            hostname = "rpi4b"
            ap_active = False

            def submit(
                self,
                _username,
                _account_password,
                _account_password_confirmation,
                _ssh_key,
                _wifi_ssid,
                _wifi_password,
                _country,
            ):
                return False, "Wi-Fi failed"

        server = self.web.SetupServer(
            ("127.0.0.1", 0), self.web.SetupHandler, FakeOnboarding(), secure=True
        )
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            with mock.patch.object(self.web, "has_usable_ethernet", return_value=False):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request(
                    "POST",
                    "/setup",
                    body=(
                        b"username=operator&account_password=long%20enough%20password"
                        b"&account_password_confirm=long%20enough%20password"
                        b"&ssh_key=ssh-ed25519%20AAAAC3NzaC1lZDI1NTE5AAAAICxEedFc5%2FSXgmFnMOYyGoi1DN2at6LTbBysqqSOOgN7%20test"
                        b"&wifi_ssid=network&wifi_password=long%20enough&wifi_country=GB"
                    ),
                )
                response = connection.getresponse()
                body = response.read()
                self.assertEqual(response.status, 202)
                self.assertIn(b"Setup failed", body)
                self.assertIn(b"Wi-Fi failed", body)
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(2)

    def test_device_name_mirrors_first_boot_machine_id_scheme(self):
        self.assertEqual(
            self.web.derive_fxroute_device_name("1af6881234567890abcdef1234567890"),
            "fxroute-1af688",
        )
        self.assertEqual(
            self.web.derive_fxroute_device_name("0123456789abcdef"),
            "fxroute-012345",
        )
        self.assertEqual(self.web.derive_fxroute_device_name("short"), "fxroute")
        self.assertEqual(self.web.derive_fxroute_device_name(""), "fxroute")
        self.assertEqual(
            self.web.fxroute_lan_url("fxroute-1af688"),
            "http://fxroute-1af688.local:8000",
        )

    def test_completion_device_name_uses_preview_hostname_in_preview(self):
        self.assertEqual(
            self.web.completion_device_name("fxroute-preview", preview=True),
            "fxroute-preview",
        )
        with mock.patch.object(
            self.web, "derive_fxroute_device_name", return_value="fxroute-1af688"
        ):
            self.assertEqual(
                self.web.completion_device_name("vim1s", preview=False),
                "fxroute-1af688",
            )

    def test_completion_html_promotes_the_dotlocal_address(self):
        page = self.web.setup_completion_html(
            "fxroute-1af688", ["192.168.1.20"], "operator"
        )
        self.assertIn("http://fxroute-1af688.local:8000", page)
        self.assertIn("Open FXRoute", page)
        self.assertIn("Copy address", page)
        self.assertIn('data-address="http://fxroute-1af688.local:8000"', page)
        self.assertIn("http://192.168.1.20:8000", page)
        self.assertIn("operator@fxroute-1af688.local", page)

    def test_completion_html_without_ips_keeps_router_fallback(self):
        page = self.web.setup_completion_html("fxroute-1af688", [], "operator")
        self.assertIn("http://fxroute-1af688.local:8000", page)
        self.assertIn("Open FXRoute", page)
        self.assertIn("Copy address", page)
        self.assertIn("router", page)

    def test_preview_completion_html_promotes_the_dotlocal_address(self):
        page = self.web.preview_completion_html("fxroute-preview")
        self.assertIn("http://fxroute-preview.local:8000", page)
        self.assertIn("Open FXRoute", page)
        self.assertIn("Copy address", page)
        self.assertIn("No system changes were made.", page)

    def test_successful_submission_links_the_dotlocal_address(self):
        class FakeOnboarding:
            hostname = "vim1s"
            ap_active = False

            def submit(self, *_args):
                return True, "Setup complete"

            def request_shutdown(self):
                server.shutdown()

        server = self.web.SetupServer(
            ("127.0.0.1", 0), self.web.SetupHandler, FakeOnboarding(), secure=True
        )
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            with (
                mock.patch.object(self.web, "has_usable_ethernet", return_value=True),
                mock.patch.object(
                    self.web,
                    "derive_fxroute_device_name",
                    return_value="fxroute-1af688",
                ),
                mock.patch.object(
                    self.web, "interface_ipv4_addresses", return_value=["192.168.1.20"]
                ),
            ):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request(
                    "POST",
                    "/setup",
                    body=(
                        b"username=operator&account_password=long%20enough%20password"
                        b"&account_password_confirm=long%20enough%20password"
                        b"&wifi_country=GB"
                    ),
                )
                response = connection.getresponse()
                body = response.read().decode("utf-8")
                self.assertEqual(response.status, 202)
                self.assertIn("Setup complete", body)
                self.assertIn("http://fxroute-1af688.local:8000", body)
                self.assertIn("Open FXRoute", body)
                self.assertIn("Copy address", body)
                self.assertIn("http://192.168.1.20:8000", body)
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(2)

    def test_ethernet_setup_console_lines_point_at_the_setup_page(self):
        self.assertEqual(
            self.web.ethernet_setup_console_lines(["192.168.178.23"]),
            [
                "FXRoute setup: open https://192.168.178.23 "
                "on another computer to configure this device."
            ],
        )
        self.assertEqual(
            self.web.ethernet_setup_console_lines(["192.168.1.20", "10.0.0.5"]),
            [
                "FXRoute setup: open https://192.168.1.20 or https://10.0.0.5 "
                "on another computer to configure this device."
            ],
        )
        self.assertEqual(self.web.ethernet_setup_console_lines([]), [])

    def test_ready_console_lines_show_ip_and_dotlocal_address(self):
        self.assertEqual(
            self.web.ready_console_lines(["192.168.178.23"], "fxroute-1af688"),
            [
                "FXRoute ready: http://192.168.178.23:8000 "
                "and http://fxroute-1af688.local:8000"
            ],
        )
        self.assertEqual(
            self.web.ready_console_lines([], "fxroute-1af688"),
            ["FXRoute ready: http://fxroute-1af688.local:8000"],
        )

    def test_announce_console_mirrors_to_consoles_without_failing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "console"
            missing = Path(directory) / "absent" / "console"
            with mock.patch.object(self.web.LOG, "info"):
                self.web.announce_console(["hello"], console_paths=(target, missing))
            self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")
            with mock.patch.object(self.web.LOG, "info"):
                self.web.announce_console([], console_paths=(target,))

    def test_serve_over_dhcp_announces_the_setup_address_on_console(self):
        onboarding = self.web.Onboarding("wlan0", "rpi4b")
        marker = mock.Mock()
        marker.exists.return_value = True
        configured = mock.Mock()
        configured.exists.return_value = False
        with (
            mock.patch.object(self.web, "MARKER_PATH", marker),
            mock.patch.object(self.web, "CONFIGURED_MARKER", configured),
            mock.patch.object(self.web, "has_usable_ethernet", return_value=True),
            mock.patch.object(self.web, "has_ethernet_carrier", return_value=False),
            mock.patch.object(
                self.web, "interface_ipv4_addresses", return_value=["192.168.178.23"]
            ),
            mock.patch.object(
                onboarding, "recover_interrupted_setup", return_value=False
            ),
            mock.patch.object(onboarding, "prime_wifi_scan"),
            mock.patch.object(self.web, "ensure_tls_certificate"),
            mock.patch.object(self.web, "announce_console") as announce,
            mock.patch.object(
                self.web, "SetupServer", side_effect=RuntimeError("stop here")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop here"):
                onboarding.serve()
        announced = announce.call_args.args[0]
        self.assertEqual(len(announced), 1)
        self.assertIn("https://192.168.178.23", announced[0])


    def test_ssh_config_without_key_allows_password_but_denies_root(self):
        config = self.web.build_ssh_config("")

        self.assertIn("PasswordAuthentication yes", config)
        self.assertIn("KbdInteractiveAuthentication yes", config)
        self.assertIn("PermitRootLogin no", config)
        self.assertIn("PubkeyAuthentication yes", config)
        self.assertNotIn("PasswordAuthentication no", config)
        self.assertNotIn("PermitRootLogin yes", config)

    def test_ssh_config_with_key_is_key_only(self):
        config = self.web.build_ssh_config(VALID_SSH_KEY)

        self.assertIn("PasswordAuthentication no", config)
        self.assertIn("KbdInteractiveAuthentication no", config)
        self.assertIn("PermitRootLogin no", config)
        self.assertIn("PubkeyAuthentication yes", config)
        self.assertNotIn("PasswordAuthentication yes", config)
        self.assertNotIn("PermitRootLogin yes", config)

    def test_configure_ssh_access_writes_conditional_config_and_enables_sshd(self):
        cases = [
            ("", "PasswordAuthentication yes"),
            ("   ", "PasswordAuthentication yes"),
            (VALID_SSH_KEY, "PasswordAuthentication no"),
        ]
        for ssh_key, expected_line in cases:
            with self.subTest(ssh_key=ssh_key[:20]):
                written = {}

                def fake_atomic_write(path, content, mode=0o600):
                    written["path"] = path
                    written["content"] = content
                    written["mode"] = mode

                calls = []

                def fake_command(args, check=True, input_text=None, timeout=None):
                    calls.append(args)
                    return self.web.subprocess.CompletedProcess(args, 0, "", "")

                with (
                    mock.patch.object(
                        self.web, "atomic_write", side_effect=fake_atomic_write
                    ),
                    mock.patch.object(self.web, "command", side_effect=fake_command),
                ):
                    self.web.configure_ssh_access(ssh_key)

                self.assertEqual(written["path"], self.web.SSH_CONFIG_PATH)
                self.assertIn(expected_line, written["content"])
                self.assertIn("PermitRootLogin no", written["content"])
                self.assertNotIn("PermitRootLogin yes", written["content"])
                flat = [" ".join(args) for args in calls]
                self.assertIn("ssh-keygen -A", flat)
                self.assertIn("sshd -t", flat)
                self.assertTrue(
                    any("systemctl enable --now" in entry for entry in flat)
                )
                self.assertTrue(
                    any("systemctl reload-or-restart" in entry for entry in flat)
                )

    def test_configure_account_forwards_ssh_key_to_ssh_access(self):
        with tempfile.TemporaryDirectory() as directory:
            account_file = Path(directory) / "account"
            onboarding = self.web.Onboarding("", "rpi4b")
            record = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir="/home/operator")
            account_file.write_text("operator\n", encoding="ascii")

            with (
                mock.patch.object(self.web, "ACCOUNT_FILE", account_file),
                mock.patch.object(self.web.pwd, "getpwnam", return_value=record),
                mock.patch.object(
                    self.web, "create_account", return_value=False
                ) as create_account,
                mock.patch.object(
                    self.web, "configure_ssh_access"
                ) as configure_ssh,
            ):
                success, _message = onboarding.configure_account(
                    "operator", "another password", VALID_SSH_KEY
                )

            self.assertTrue(success)
            create_account.assert_called_once_with(
                "operator",
                "another password",
                VALID_SSH_KEY,
                allow_existing=True,
            )
            configure_ssh.assert_called_once_with(VALID_SSH_KEY)

    def test_configure_account_without_key_keeps_password_ssh_open(self):
        with tempfile.TemporaryDirectory() as directory:
            account_file = Path(directory) / "account"
            onboarding = self.web.Onboarding("", "rpi4b")
            record = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir="/home/operator")
            account_file.write_text("operator\n", encoding="ascii")

            with (
                mock.patch.object(self.web, "ACCOUNT_FILE", account_file),
                mock.patch.object(self.web.pwd, "getpwnam", return_value=record),
                mock.patch.object(
                    self.web, "create_account", return_value=False
                ),
                mock.patch.object(
                    self.web, "configure_ssh_access"
                ) as configure_ssh,
            ):
                success, _message = onboarding.configure_account(
                    "operator", "another password", ""
                )

            self.assertTrue(success)
            configure_ssh.assert_called_once_with("")


if __name__ == "__main__":
    unittest.main()
