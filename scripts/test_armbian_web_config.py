#!/usr/bin/env python3
"""Behavior tests for the headless Armbian Wi-Fi setup service."""

import importlib.util
import base64
import hashlib
import http.client
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
        salt = b"test-setup-salt!"
        iterations = 600000
        verifier = "pbkdf2-sha256${}${}${}".format(
            iterations,
            salt.hex(),
            hashlib.pbkdf2_hmac(
                "sha256", b"setup-pass", salt, iterations
            ).hex(),
        )
        cls.password_environment = mock.patch.dict(
            "os.environ",
            {
                "ARMBIAN_WEB_CONFIG_AP_PASSWORD_VERIFIER_B64": base64.b64encode(
                    verifier.encode("ascii")
                ).decode("ascii"),
            },
        )
        cls.password_environment.start()
        cls.web = load_web_config()

    def setUp(self):
        with self.web._setup_password_attempt_lock:
            self.web._setup_password_attempts.clear()

    @classmethod
    def tearDownClass(cls):
        cls.password_environment.stop()

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

    def test_setup_validation_rejects_unsafe_or_unusable_values(self):
        with self.assertRaises(ValueError):
            self.web.validate_setup("", "", "GB")
        with self.assertRaises(ValueError):
            self.web.validate_setup("network", "short", "GB")
        with self.assertRaises(ValueError):
            self.web.validate_setup("network", "long enough", "Germany")

        self.assertEqual(
            self.web.validate_setup("network", "long enough", "de"),
            ("network", "long enough", "DE"),
        )

        with self.assertRaises(ValueError):
            self.web.validate_setup_password("wrong-setup-pass")

    def test_account_validation_requires_a_real_user_password_and_ssh_key(self):
        with self.assertRaises(ValueError):
            self.web.validate_account(
                "", "long enough password", "long enough password", "ssh-ed25519 AAAA"
            )
        with self.assertRaises(ValueError):
            self.web.validate_account(
                "root", "long enough password", "long enough password", "ssh-ed25519 AAAA"
            )
        with self.assertRaises(ValueError):
            self.web.validate_account("operator", "short", "short", "ssh-ed25519 AAAA")
        with self.assertRaises(ValueError):
            self.web.validate_account(
                "operator", "long enough password", "different password", "ssh-ed25519 AAAA"
            )
        with self.assertRaises(ValueError):
            self.web.validate_account(
                "operator", "long enough password", "long enough password", "not-a-key"
            )
        with self.assertRaises(ValueError):
            self.web.validate_account(
                "operator", "long enough password", "long enough password", "ssh-ed25519 AAAA"
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
                    "setup-pass",
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
                "setup-pass",
            )

        self.assertFalse(success)
        self.assertEqual(message, "Wired Ethernet is no longer available; provide Wi-Fi")
        mark_configured.assert_not_called()

    def test_setup_page_matches_the_pinned_ssid_and_ap_address(self):
        page = self.web.setup_page("rpi4b")

        self.assertIn("rpi4b-armbiansetup", page)
        self.assertIn("10.42.0.1", page)
        self.assertIn("printed during the image build", page)
        self.assertNotIn("setup-pass", page)

    def test_setup_ssid_uses_the_runtime_hostname(self):
        page = self.web.setup_page("rpi5b")

        self.assertIn("rpi5b-armbiansetup", page)
        self.assertNotIn("rpi4b-armbiansetup", page)

    def test_setup_password_attempts_are_rate_limited(self):
        with mock.patch.object(self.web, "SETUP_PASSWORD_ATTEMPT_LIMIT", 1):
            with self.assertRaises(ValueError):
                self.web.validate_setup_password("wrong-setup-pass")
            with self.assertRaisesRegex(ValueError, "Too many setup password attempts"):
                self.web.validate_setup_password("wrong-setup-pass")

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
        self.assertIn("DHCP", page)
        self.assertIn("Temporary setup password", page)
        self.assertNotIn("setup-pass", page)

    def test_ap_setup_page_posts_account_credentials_over_tls(self):
        page = self.web.setup_page("rpi4b", ethernet=False, ap_active=True)
        self.assertIn('action="https://10.42.0.1/setup"', page)

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
                    "setup-pass",
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
                _setup_password,
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
                        b"&setup_password=setup-pass"
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
                _setup_password,
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
                        b"&setup_password=setup-pass"
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


if __name__ == "__main__":
    unittest.main()
