#!/usr/bin/env python3
"""Provide the temporary Armbian first-boot network and account setup."""

from __future__ import annotations

import html
import hashlib
import logging
import os
from pathlib import Path
import pwd
import re
import secrets
import signal
import ssl
import subprocess
import threading
import tempfile
import time
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


AP_ADDRESS = "10.42.0.1"
AP_SSID_SUFFIX = "-armbiansetup"
CONFIGURED_MARKER = Path("/var/lib/armbian-web-config/configured")
ACCOUNT_FILE = Path("/var/lib/armbian-web-config/account")
HOSTAPD_CONFIG = Path("/run/armbian-web-config/hostapd.conf")
DNSMASQ_CONFIG = Path("/run/armbian-web-config/dnsmasq.conf")
MARKER_PATH = Path("/root/.not_logged_in_yet")
NETPLAN_PATH = Path("/etc/netplan/30-wifis-dhcp.yaml")
SSH_CONFIG_PATH = Path("/etc/ssh/sshd_config.d/90-fxroute-armbian.conf")
RUN_DIR = Path("/run/armbian-web-config")
TLS_CERT_PATH = RUN_DIR / "setup.crt"
TLS_KEY_PATH = RUN_DIR / "setup.key"
AP_PASSWORD_VERIFIER_ENV = "ARMBIAN_WEB_CONFIG_AP_PASSWORD_VERIFIER_B64"
AP_PSK_ENV = "ARMBIAN_WEB_CONFIG_AP_PSK"
AP_SSID_ENV = "ARMBIAN_WEB_CONFIG_AP_SSID"
AP_PASSWORD_ITERATIONS = 600000

USERNAME_PATTERN = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")
SSH_KEY_PATTERN = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521))[ \t]+"
    r"[A-Za-z0-9+/=]+(?:[ \t]+[^\r\n]*)?$"
)

LOG = logging.getLogger("armbian-web-config")


def ethernet_interface_names(sys_class_net: str = "/sys/class/net") -> list[str]:
    try:
        interfaces = sorted(Path(sys_class_net).iterdir())
    except OSError:
        return []

    names = []
    for interface in interfaces:
        name = interface.name
        if name == "lo" or name.startswith(("wl", "wlan", "vir", "br", "docker", "veth")):
            continue
        if name.startswith(("eth", "en", "end", "lan", "wan")):
            names.append(name)
    return names


def has_ethernet_carrier(sys_class_net: str = "/sys/class/net") -> bool:
    """Return whether a physical wired interface currently has a carrier."""

    for name in ethernet_interface_names(sys_class_net):
        interface = Path(sys_class_net) / name
        try:
            if (interface / "carrier").read_text(encoding="ascii").strip() == "1":
                return True
        except (OSError, UnicodeError):
            continue
    return False


def has_usable_ethernet(sys_class_net: str = "/sys/class/net") -> bool:
    """Return whether a wired interface has carrier and a global IPv4 address."""

    for name in ethernet_interface_names(sys_class_net):
        interface = Path(sys_class_net) / name
        try:
            if (interface / "carrier").read_text(encoding="ascii").strip() != "1":
                continue
            result = command(
                ["ip", "-4", "addr", "show", "dev", name, "scope", "global"],
                check=False,
            )
        except (OSError, UnicodeError):
            continue
        if result.returncode == 0 and re.search(
            r"\binet\s+[0-9.]+/", result.stdout
        ):
            return True
    return False


def wifi_interface_names(iw_output: str) -> list[str]:
    """Extract interface names from the stable ``iw dev`` output format."""

    names = []
    for line in iw_output.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "Interface":
            names.append(fields[1])
    return names


def find_wifi_interface() -> str | None:
    try:
        result = subprocess.run(
            ["iw", "dev"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return next(iter(wifi_interface_names(result.stdout)), None)


def hostname() -> str:
    try:
        value = Path("/etc/hostname").read_text(encoding="utf-8").strip()
    except OSError:
        value = "armbian"
    value = re.sub(r"[^A-Za-z0-9-]", "-", value).strip("-")
    return value or "armbian"


def onboarding_ssid(hostname_value: str) -> str:
    suffix = "-armbiansetup"
    prefix = re.sub(r"[^A-Za-z0-9-]", "-", hostname_value).strip("-")
    return f"{prefix[: 32 - len(suffix)] or 'armbian'}{suffix}"


def setup_ap_ssid(hostname_value: str) -> str:
    configured = os.environ.get(AP_SSID_ENV, "")
    if configured:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,32}", configured):
            raise RuntimeError("The setup AP SSID metadata is invalid")
        return configured
    return onboarding_ssid(hostname_value)


def setup_password_verifier() -> tuple[int, bytes, bytes]:
    encoded = os.environ.get(AP_PASSWORD_VERIFIER_ENV, "")
    try:
        verifier = base64.b64decode(encoded, validate=True).decode("ascii")
        algorithm, iterations_text, salt_text, digest_text = verifier.split("$")
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_text)
        digest = bytes.fromhex(digest_text)
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("The Wi-Fi setup password verifier is invalid") from None
    if (
        algorithm != "pbkdf2-sha256"
        or iterations != AP_PASSWORD_ITERATIONS
        or len(salt) != 16
        or len(digest) != 32
    ):
        raise RuntimeError("The Wi-Fi setup password verifier is invalid")
    return iterations, salt, digest


def setup_ap_psk() -> str:
    psk = os.environ.get(AP_PSK_ENV, "")
    if not re.fullmatch(r"[0-9a-f]{64}", psk):
        raise RuntimeError("The setup AP key metadata is invalid")
    return psk


def validate_setup_password(setup_password: str) -> None:
    try:
        password_bytes = setup_password.encode("ascii")
        iterations, salt, expected = setup_password_verifier()
    except (UnicodeEncodeError, RuntimeError) as error:
        raise ValueError(str(error)) from None
    actual = hashlib.pbkdf2_hmac("sha256", password_bytes, salt, iterations)
    if not secrets.compare_digest(actual, expected):
        raise ValueError("The temporary setup password is incorrect")


def yaml_string(value: str) -> str:
    """Quote a scalar using JSON's compatible YAML double-quoted form."""

    import json

    return json.dumps(value, ensure_ascii=True)


def build_wifi_netplan(
    interface: str,
    ssid: str,
    password: str,
    country: str,
) -> str:
    """Build the networkd config used by Armbian's first-login helper."""

    access_point = f"        {yaml_string(ssid)}: {{}}\n"
    if password:
        access_point = (
            f"        {yaml_string(ssid)}:\n"
            f"          password: {yaml_string(password)}\n"
        )
    return (
        "network:\n"
        "  version: 2\n"
        "  renderer: networkd\n"
        "  wifis:\n"
        f"    {yaml_string(interface)}:\n"
        f"      regulatory-domain: {country}\n"
        "      dhcp4: true\n"
        "      dhcp6: true\n"
        "      dhcp4-overrides:\n"
        "        route-metric: 600\n"
        "      dhcp6-overrides:\n"
        "        route-metric: 600\n"
        "      access-points:\n"
        f"{access_point}"
    )


def validate_setup(ssid: str, password: str, country: str) -> tuple[str, str, str]:
    if not ssid or any(char in ssid for char in "\x00\r\n"):
        raise ValueError("Wi-Fi network name is required")
    if len(ssid.encode("utf-8")) > 32:
        raise ValueError("Wi-Fi network name must be at most 32 bytes")
    if any(char in password for char in "\x00\r\n"):
        raise ValueError("Wi-Fi password contains an invalid character")
    password_length = len(password.encode("utf-8"))
    if password and not 8 <= password_length <= 63:
        raise ValueError("Wi-Fi password must be 8 to 63 bytes")
    country = country.upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("Wi-Fi country code must contain two letters")
    return ssid, password, country


def validate_ssh_public_key(ssh_key: str) -> None:
    if any(char in ssh_key for char in "\x00\r\n") or not SSH_KEY_PATTERN.fullmatch(ssh_key):
        raise ValueError("Enter one supported OpenSSH public key")
    result = command(
        ["ssh-keygen", "-lf", "/dev/stdin"],
        check=False,
        input_text=f"{ssh_key}\n",
    )
    if result.returncode != 0:
        raise ValueError("The SSH public key is not valid")


def validate_account(
    username: str,
    password: str,
    password_confirmation: str,
    ssh_key: str,
) -> tuple[str, str, str]:
    username = username.lower()
    if not USERNAME_PATTERN.fullmatch(username) or username == "root":
        raise ValueError("User name must start with a letter or underscore and contain only letters, numbers, dots, underscores, or hyphens")
    if any(char in password for char in "\x00\r\n") or ":" in password:
        raise ValueError("Account password contains an invalid character")
    password_length = len(password.encode("utf-8"))
    if password_length < 12:
        raise ValueError("Account password must be at least 12 bytes")
    if password != password_confirmation:
        raise ValueError("Account passwords do not match")
    validate_ssh_public_key(ssh_key)
    return username, password, ssh_key


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def command(
    args: list[str],
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        capture_output=True,
        text=True,
        input=input_text,
    )


def certificate_pair_is_usable(cert_path: Path, key_path: Path) -> bool:
    if (
        cert_path.is_symlink()
        or key_path.is_symlink()
        or not cert_path.is_file()
        or not key_path.is_file()
    ):
        return False
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(str(cert_path), str(key_path))
    except (OSError, ssl.SSLError):
        return False
    return True


def ensure_tls_certificate(hostname_value: str) -> None:
    RUN_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    if certificate_pair_is_usable(TLS_CERT_PATH, TLS_KEY_PATH):
        os.chmod(TLS_KEY_PATH, 0o600)
        return

    with tempfile.TemporaryDirectory(prefix=".tls-", dir=RUN_DIR) as temporary_dir:
        temporary_cert = Path(temporary_dir) / "setup.crt"
        temporary_key = Path(temporary_dir) / "setup.key"
        command(
            [
                "openssl",
                "req",
                "-batch",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(temporary_key),
                "-out",
                str(temporary_cert),
                "-days",
                "36500",
                "-subj",
                f"/CN={hostname_value}",
            ]
        )
        os.chmod(temporary_key, 0o600)
        os.chmod(temporary_cert, 0o644)
        if not certificate_pair_is_usable(temporary_cert, temporary_key):
            raise RuntimeError("The generated setup certificate is invalid")
        os.replace(temporary_key, TLS_KEY_PATH)
        os.replace(temporary_cert, TLS_CERT_PATH)


def link_has_ipv4(interface: str) -> bool:
    result = command(
        ["ip", "-4", "addr", "show", "dev", interface, "scope", "global"],
        check=False,
    )
    return re.search(r"\binet\s+[0-9.]+/", result.stdout) is not None


def wifi_is_connected(interface: str) -> bool:
    result = command(["iw", "dev", interface, "link"], check=False)
    return "Connected to " in result.stdout


def should_start_access_point(ethernet_carrier: bool, interface: str) -> bool:
    return not ethernet_carrier and bool(interface)


def stored_account_username() -> str:
    if ACCOUNT_FILE.is_symlink():
        raise RuntimeError("Refusing symlinked account metadata")
    if not ACCOUNT_FILE.exists():
        return ""
    username = ACCOUNT_FILE.read_text(encoding="utf-8").strip()
    if not USERNAME_PATTERN.fullmatch(username) or username == "root":
        raise RuntimeError("The stored account metadata is invalid")
    return username


def create_account(
    username: str,
    password: str,
    ssh_key: str,
    allow_existing: bool = False,
) -> bool:
    created = False
    try:
        try:
            record = pwd.getpwnam(username)
        except KeyError:
            command(
                [
                    "useradd",
                    "--create-home",
                    "--user-group",
                    "--shell",
                    "/bin/bash",
                    username,
                ]
            )
            created = True
            try:
                record = pwd.getpwnam(username)
            except KeyError:
                raise RuntimeError("The new user account could not be read") from None
        else:
            if not allow_existing:
                raise RuntimeError("That user name is already in use")

        if record.pw_uid < 1000 or record.pw_uid == 65534:
            raise RuntimeError("That user name is already used by a system account")
        home = Path(record.pw_dir)
        if not home.is_dir():
            raise RuntimeError("The new user home directory is unavailable")

        command(["chpasswd"], input_text=f"{username}:{password}\n")
        for group in ("sudo", "wheel", "audio"):
            if command(["getent", "group", group], check=False).returncode == 0:
                command(["usermod", "-aG", group, username])

        ssh_dir = home / ".ssh"
        authorized_keys = ssh_dir / "authorized_keys"
        if ssh_dir.is_symlink() or authorized_keys.is_symlink():
            raise RuntimeError("Refusing symlinked SSH configuration")
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ssh_dir, 0o700)
        os.chown(ssh_dir, record.pw_uid, record.pw_gid)
        if authorized_keys.exists() and not authorized_keys.is_file():
            raise RuntimeError("The SSH authorized_keys path is not a file")
        atomic_write(authorized_keys, f"{ssh_key}\n")
        os.chown(authorized_keys, record.pw_uid, record.pw_gid)

        if command(["getent", "passwd", username], check=False).returncode != 0:
            raise RuntimeError("The new user account could not be verified")
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        if created:
            command(["userdel", "--remove", username], check=False)
        raise
    return created


def configure_ssh_access() -> None:
    atomic_write(
        SSH_CONFIG_PATH,
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
        "PermitRootLogin no\n"
        "PubkeyAuthentication yes\n",
        0o644,
    )
    command(["ssh-keygen", "-A"])
    check = command(["sshd", "-t"], check=False)
    if check.returncode != 0:
        raise RuntimeError("The SSH configuration is invalid")
    for service in ("ssh.service", "sshd.service"):
        if command(["systemctl", "enable", "--now", service], check=False).returncode == 0:
            if command(["systemctl", "reload-or-restart", service], check=False).returncode != 0:
                raise RuntimeError("The SSH service could not be restarted")
            return
    raise RuntimeError("The SSH service could not be enabled")


class Onboarding:
    def __init__(self, interface: str, hostname_value: str):
        self.interface = interface
        self.hostname = hostname_value
        self.ap_processes: list[subprocess.Popen[bytes]] = []
        self.ap_active = False
        self.server: ThreadingHTTPServer | None = None
        self.secure_server: ThreadingHTTPServer | None = None
        self.shutdown_event = threading.Event()
        self.configure_lock = threading.RLock()
        self.ethernet_loss_deadline: float | None = None

    def mark_configured(self) -> bool:
        with self.configure_lock:
            if CONFIGURED_MARKER.exists():
                return False
            atomic_write(CONFIGURED_MARKER, "configured\n")
            self.shutdown_event.set()
            return True

    def configure_account(
        self, username: str, password: str, ssh_key: str
    ) -> tuple[bool, str]:
        with self.configure_lock:
            if CONFIGURED_MARKER.exists():
                return False, "Setup has already been completed"
            try:
                previous = stored_account_username()
            except (OSError, RuntimeError) as error:
                return False, str(error)
            if previous and previous != username:
                return False, "An account has already been selected for this device"
            existing_record = None
            if previous:
                try:
                    existing_record = pwd.getpwnam(previous)
                except KeyError:
                    pass
                else:
                    if existing_record.pw_uid < 1000 or existing_record.pw_uid == 65534:
                        return False, "The selected account is not a normal user account"

            if not previous:
                try:
                    pwd.getpwnam(username)
                except KeyError:
                    pass
                else:
                    return False, "That user name already exists; choose another"

            created = False
            account_written = bool(previous)
            try:
                # Record the claim before useradd so a power loss leaves a
                # recoverable intent instead of an untracked privileged user.
                if not previous:
                    atomic_write(ACCOUNT_FILE, f"{username}\n")
                    account_written = True
                created = bool(
                    create_account(
                        username,
                        password,
                        ssh_key,
                        allow_existing=existing_record is not None,
                    )
                )
                configure_ssh_access()
            except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
                if account_written and not previous:
                    ACCOUNT_FILE.unlink(missing_ok=True)
                if created:
                    command(["userdel", "--remove", username], check=False)
                return False, str(error)
            return True, "Account configured"

    def start_access_point(self) -> None:
        RUN_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        ssid = setup_ap_ssid(self.hostname)
        psk = setup_ap_psk()
        if len(ssid.encode("utf-8")) > 32:
            raise RuntimeError("Armbian setup SSID is longer than 32 bytes")
        atomic_write(
            HOSTAPD_CONFIG,
            "interface="
            + self.interface
            + "\n"
            + "driver=nl80211\n"
            + f"ssid={ssid}\n"
            + "hw_mode=g\n"
            + "channel=6\n"
            + "auth_algs=1\n"
            + "wpa=2\n"
            + f"wpa_psk={psk}\n"
            + "wpa_key_mgmt=WPA-PSK\n"
            + "rsn_pairwise=CCMP\n",
            0o600,
        )
        atomic_write(
            DNSMASQ_CONFIG,
            "bind-interfaces\n"
            + f"interface={self.interface}\n"
            + f"listen-address={AP_ADDRESS}\n"
            + "no-resolv\n"
            + "dhcp-range=10.42.0.10,10.42.0.100,255.255.255.0,12h\n"
            + f"dhcp-option=3,{AP_ADDRESS}\n"
            + f"dhcp-option=6,{AP_ADDRESS}\n"
            + f"address=/#/{AP_ADDRESS}\n",
            0o600,
        )
        command(["ip", "link", "set", "dev", self.interface, "up"])
        command(["ip", "addr", "flush", "dev", self.interface], check=False)
        command(["ip", "addr", "add", f"{AP_ADDRESS}/24", "dev", self.interface])
        self.ap_active = True
        self.ap_processes = []
        try:
            self.ap_processes.append(subprocess.Popen(["hostapd", str(HOSTAPD_CONFIG)]))
            self.ap_processes.append(
                subprocess.Popen(
                    ["dnsmasq", "--no-daemon", f"--conf-file={DNSMASQ_CONFIG}"]
                )
            )
        except OSError:
            self.stop_access_point()
            raise
        time.sleep(1)
        if any(process.poll() is not None for process in self.ap_processes):
            self.stop_access_point()
            raise RuntimeError("The setup access point could not be started")
        LOG.info("setup access point %s is available at http://%s", ssid, AP_ADDRESS)

    def stop_access_point(self) -> None:
        was_active = self.ap_active
        self.ap_active = False
        for process in reversed(self.ap_processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(self.ap_processes):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self.ap_processes = []
        if was_active:
            command(["ip", "addr", "flush", "dev", self.interface], check=False)
            command(["ip", "link", "set", "dev", self.interface, "down"], check=False)

    def wait_for_network(self, timeout: int = 60) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.shutdown_event.is_set():
                return False
            if has_usable_ethernet():
                return True
            if wifi_is_connected(self.interface) and link_has_ipv4(self.interface):
                return True
            time.sleep(1)
        return False

    def wait_for_ethernet(self, timeout: int = 60) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.shutdown_event.is_set():
                return False
            if has_usable_ethernet():
                return True
            if not has_ethernet_carrier():
                return False
            time.sleep(1)
        return False

    def configure_wifi(self, ssid: str, password: str, country: str) -> tuple[bool, str]:
        try:
            ssid, password, country = validate_setup(ssid, password, country)
        except ValueError as error:
            return False, str(error)

        with self.configure_lock:
            if has_usable_ethernet():
                return False, "Wired Ethernet detected"
            previous = NETPLAN_PATH.read_bytes() if NETPLAN_PATH.exists() else None
            self.stop_access_point()
            try:
                atomic_write(
                    NETPLAN_PATH,
                    build_wifi_netplan(self.interface, ssid, password, country),
                )
                command(["netplan", "apply"])
                if not self.wait_for_network():
                    raise RuntimeError("The device did not connect to the Wi-Fi network")
                return True, "Wi-Fi configured"
            except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
                if previous is None:
                    NETPLAN_PATH.unlink(missing_ok=True)
                else:
                    NETPLAN_PATH.write_bytes(previous)
                    os.chmod(NETPLAN_PATH, 0o600)
                command(["netplan", "apply"], check=False)
                if not has_usable_ethernet():
                    try:
                        self.start_access_point()
                    except (OSError, RuntimeError, subprocess.CalledProcessError):
                        LOG.exception("could not restore the setup access point")
                return False, str(error)

    def submit(
        self,
        username: str,
        account_password: str,
        account_password_confirmation: str,
        ssh_key: str,
        wifi_ssid: str,
        wifi_password: str,
        country: str,
        setup_password: str,
    ) -> tuple[bool, str]:
        with self.configure_lock:
            if CONFIGURED_MARKER.exists():
                return False, "Setup has already been completed"
            try:
                validate_setup_password(setup_password)
                username, account_password, ssh_key = validate_account(
                    username,
                    account_password,
                    account_password_confirmation,
                    ssh_key,
                )
                wifi_requested = bool(wifi_ssid or wifi_password)
                if wifi_requested:
                    wifi_ssid, wifi_password, country = validate_setup(
                        wifi_ssid, wifi_password, country
                    )
                elif not has_usable_ethernet():
                    raise ValueError("Connect Ethernet or provide a Wi-Fi network name")
            except ValueError as error:
                return False, str(error)

            success, message = self.configure_account(username, account_password, ssh_key)
            if not success:
                return False, message
            if wifi_requested:
                success, message = self.configure_wifi(wifi_ssid, wifi_password, country)
                if not success:
                    return False, message
            elif not has_usable_ethernet():
                return False, "Wired Ethernet is no longer available; provide Wi-Fi"
            if not self.mark_configured():
                return False, "Setup has already been completed"
            return True, "Setup complete"

    def recover_interrupted_setup(self) -> bool:
        if not (stored_account_username() and NETPLAN_PATH.exists()):
            return False
        if not has_usable_ethernet() and not (
            self.interface and self.wait_for_network(timeout=60)
        ):
            NETPLAN_PATH.unlink(missing_ok=True)
            command(["netplan", "apply"], check=False)
            return False
        self.mark_configured()
        return True

    def request_shutdown(self) -> None:
        self.shutdown_event.set()
        for server in (self.server, self.secure_server):
            if server is not None:
                threading.Thread(target=server.shutdown, daemon=True).start()

    def monitor_ethernet(self) -> None:
        while not self.shutdown_event.wait(1):
            with self.configure_lock:
                if has_usable_ethernet():
                    self.ethernet_loss_deadline = None
                    if self.ap_active:
                        LOG.info("wired Ethernet detected; stopping the setup access point")
                        self.stop_access_point()
                    continue
                if self.ap_active:
                    if len(self.ap_processes) == 2 and all(
                        process.poll() is None for process in self.ap_processes
                    ):
                        continue
                    LOG.warning("setup access point process exited; restarting it")
                    self.stop_access_point()
                    try:
                        self.start_access_point()
                    except (OSError, RuntimeError, subprocess.CalledProcessError):
                        LOG.exception("could not restart the setup access point")
                        self.shutdown_event.wait(5)
                    continue
                if not self.interface:
                    continue
                if has_ethernet_carrier():
                    if self.ethernet_loss_deadline is None:
                        self.ethernet_loss_deadline = time.monotonic() + 60
                        continue
                    if time.monotonic() < self.ethernet_loss_deadline:
                        continue
                self.ethernet_loss_deadline = None
                try:
                    LOG.info("wired Ethernet is unavailable; starting the setup access point")
                    self.start_access_point()
                except (OSError, RuntimeError, subprocess.CalledProcessError):
                    LOG.exception("could not start the setup access point")
                    self.request_shutdown()

    def serve(self) -> int:
        if not MARKER_PATH.exists() or CONFIGURED_MARKER.exists():
            return 0
        ethernet = has_usable_ethernet()
        if not ethernet and has_ethernet_carrier():
            LOG.info("wired Ethernet carrier detected; waiting for DHCP")
            ethernet = self.wait_for_ethernet()
        if self.recover_interrupted_setup():
            return 0
        setup_password_verifier()
        if not should_start_access_point(ethernet, self.interface):
            if not ethernet:
                raise RuntimeError("No Wi-Fi interface is available for headless setup")
            LOG.info("wired Ethernet detected; serving setup over the DHCP network")
        else:
            self.start_access_point()
        ensure_tls_certificate(self.hostname)
        self.server = SetupServer(("0.0.0.0", 80), SetupHandler, self)
        self.secure_server = SetupServer(
            ("0.0.0.0", 443), SetupHandler, self, secure=True
        )
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(str(TLS_CERT_PATH), str(TLS_KEY_PATH))
        self.secure_server.socket = tls_context.wrap_socket(
            self.secure_server.socket, server_side=True
        )
        secure_thread = threading.Thread(
            target=self.secure_server.serve_forever, daemon=True
        )
        secure_thread.start()
        monitor = threading.Thread(target=self.monitor_ethernet, daemon=True)
        monitor.start()
        try:
            self.server.serve_forever()
        finally:
            self.shutdown_event.set()
            self.secure_server.shutdown()
            self.server.server_close()
            self.secure_server.server_close()
            secure_thread.join(5)
            self.stop_access_point()
        return 0 if CONFIGURED_MARKER.exists() else 1


def setup_page(
    hostname_value: str,
    error: str = "",
    ethernet: bool = False,
    ap_active: bool = True,
) -> str:
    escaped_error = ""
    if error:
        escaped_error = f'<p class="error">{html.escape(error)}</p>'
    ssid = html.escape(setup_ap_ssid(hostname_value))
    network_hint = (
        "Ethernet is connected. Open this device's DHCP address in a browser; "
        "the setup service remains available on the wired network over HTTPS."
        if ethernet
        else (
            f"Connect to <strong>{ssid}</strong>, then open "
            f"<strong>http://{AP_ADDRESS}</strong>. The form is submitted over HTTPS."
        )
    )
    ap_hint = (
        '<p class="hint">Enter the temporary setup password printed during '
        "the image build for the Wi-Fi connection and this form."
        "</p>"
    )
    wifi_required = "" if ethernet else " required"
    form_action = (
        f"https://{AP_ADDRESS}/setup" if ap_active else "/setup"
    )
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FXRoute first boot</title>
<style>body{{font:16px sans-serif;max-width:38rem;margin:3rem auto;padding:0 1rem;color:#20252b}}
label{{display:block;margin:1rem 0 .35rem;font-weight:600}}input,textarea{{box-sizing:border-box;width:100%;padding:.7rem;font:inherit}}
button{{margin-top:1.5rem;padding:.7rem 1.2rem;font:inherit;font-weight:600}}.hint{{color:#56616b}}
.error{{padding:.7rem;background:#fee;color:#900}}</style></head>
<body><h1>FXRoute first boot</h1>
<p class="hint">{network_hint} Ethernet is preferred automatically when a cable is connected.</p>
{escaped_error}
<form method="post" action="{form_action}">
<h2>Administrator account</h2>
<label for="username">User name</label><input id="username" name="username" maxlength="32" pattern="[A-Za-z_][A-Za-z0-9_.-]*" required autofocus>
<label for="account_password">Account password</label><input id="account_password" name="account_password" type="password" minlength="12" autocomplete="new-password" required>
<label for="account_password_confirm">Repeat account password</label><input id="account_password_confirm" name="account_password_confirm" type="password" minlength="12" autocomplete="new-password" required>
<label for="ssh_key">SSH public key</label><textarea id="ssh_key" name="ssh_key" rows="3" placeholder="ssh-ed25519 AAAA..." required></textarea>
<label for="setup_password">Temporary setup password</label><input id="setup_password" name="setup_password" type="password" autocomplete="off" required>
<h2>Wi-Fi</h2>
<p class="hint">Wi-Fi is required when no Ethernet cable is connected and optional otherwise.</p>
<label for="wifi_ssid">Wi-Fi network name</label><input id="wifi_ssid" name="wifi_ssid" maxlength="32"{wifi_required}>
<label for="wifi_password">Wi-Fi password</label><input id="wifi_password" name="wifi_password" type="password" autocomplete="off">
<label for="wifi_country">Two-letter country code</label><input id="wifi_country" name="wifi_country" value="GB" maxlength="2" pattern="[A-Za-z]{{2}}" required>
<button type="submit">Finish FXRoute setup</button></form>
{ap_hint}</body></html>"""


class SetupServer(ThreadingHTTPServer):
    def __init__(self, address, handler, onboarding: Onboarding, secure: bool = False):
        super().__init__(address, handler)
        self.onboarding = onboarding
        self.secure = secure


class SetupHandler(BaseHTTPRequestHandler):
    server: SetupServer

    def send_content(self, content: str, status: int = 200, content_type: str = "text/html") -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def begin_setup_response(self) -> None:
        self.close_connection = True
        self.send_response(202)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(
            b"<h1>Applying FXRoute setup</h1>"
            b"<p>The account and network are being configured.</p>"
        )
        self.wfile.flush()

    def redirect_to_tls(self, path: str = "/") -> None:
        host = self.connection.getsockname()[0]
        if ":" in host:
            host = f"[{host}]"
        self.send_response(302)
        self.send_header("Location", f"https://{host}{path}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if not self.server.secure and has_usable_ethernet():
            self.redirect_to_tls()
            return
        path = urlsplit(self.path).path
        if path in ("/", "/generate_204", "/hotspot-detect.html", "/ncsi.txt"):
            self.send_content(
                setup_page(
                    self.server.onboarding.hostname,
                    ethernet=has_usable_ethernet(),
                    ap_active=self.server.onboarding.ap_active,
                )
            )
        else:
            self.send_content("Not found\n", 404, "text/plain")

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/setup":
            self.send_content("Not found\n", 404, "text/plain")
            return
        if not self.server.secure:
            self.close_connection = True
            self.send_content("Use HTTPS to submit setup credentials\n", 400, "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 64 * 1024:
                raise ValueError("Request is too large")
            values = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            username = values.get("username", [""])[0]
            account_password = values.get("account_password", [""])[0]
            account_password_confirm = values.get("account_password_confirm", [""])[0]
            ssh_key = values.get("ssh_key", [""])[0]
            wifi_ssid = values.get("wifi_ssid", [""])[0]
            wifi_password = values.get("wifi_password", [""])[0]
            country = values.get("wifi_country", [""])[0]
            setup_password = values.get("setup_password", [""])[0]
            validate_setup_password(setup_password)
            validate_account(username, account_password, account_password_confirm, ssh_key)
            if wifi_ssid or wifi_password:
                validate_setup(wifi_ssid, wifi_password, country)
            elif not has_usable_ethernet():
                raise ValueError("Connect Ethernet or provide a Wi-Fi network name")
        except (ValueError, UnicodeDecodeError) as error:
            self.send_content(
                setup_page(
                    self.server.onboarding.hostname,
                    str(error),
                    ethernet=has_usable_ethernet(),
                    ap_active=self.server.onboarding.ap_active,
                ),
                400,
            )
            return

        self.begin_setup_response()
        try:
            success, message = self.server.onboarding.submit(
                username,
                account_password,
                account_password_confirm,
                ssh_key,
                wifi_ssid,
                wifi_password,
                country,
                setup_password,
            )
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            success, message = False, str(error)
        if success:
            # The Wi-Fi AP may already be gone, so stop both listeners before
            # attempting the optional final response write.
            self.server.onboarding.request_shutdown()
            self.wfile.write(
                b"<p>Setup complete. You can connect over SSH when this page closes.</p>"
            )
            self.wfile.flush()
        else:
            LOG.warning("Wi-Fi setup was not completed: %s", message)
            self.wfile.write(
                f"<h2>Setup failed</h2><p>{html.escape(message)}</p>".encode("utf-8")
            )
            self.wfile.flush()

    def log_message(self, format: str, *args) -> None:
        LOG.info("%s - %s", self.address_string(), format % args)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[armbian-web-config] %(message)s")
    onboarding = Onboarding(find_wifi_interface() or "", hostname())

    def stop(_signum, _frame) -> None:
        onboarding.request_shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return onboarding.serve()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        LOG.error("%s", error)
        onboarding.stop_access_point()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
