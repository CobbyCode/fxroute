#!/usr/bin/env python3
"""Provide the temporary Armbian first-boot network and account setup."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
from pathlib import Path
import pwd
import re
import signal
import ssl
import subprocess
import threading
import tempfile
import time
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
WIFI_SCAN_TIMEOUT = 15
WIFI_SCAN_MIN_INTERVAL = 2.0

USERNAME_PATTERN = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")
SSH_KEY_PATTERN = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521))[ \t]+"
    r"[A-Za-z0-9+/=]+(?:[ \t]+[^\r\n]*)?$"
)

LOG = logging.getLogger("armbian-web-config")

COUNTRY_OPTIONS = (
    ("Afghanistan", "AF"),
    ("Albania", "AL"),
    ("Algeria", "DZ"),
    ("Andorra", "AD"),
    ("Angola", "AO"),
    ("Antigua and Barbuda", "AG"),
    ("Argentina", "AR"),
    ("Armenia", "AM"),
    ("Australia", "AU"),
    ("Austria", "AT"),
    ("Azerbaijan", "AZ"),
    ("Bahamas", "BS"),
    ("Bahrain", "BH"),
    ("Bangladesh", "BD"),
    ("Barbados", "BB"),
    ("Belarus", "BY"),
    ("Belgium", "BE"),
    ("Belize", "BZ"),
    ("Benin", "BJ"),
    ("Bhutan", "BT"),
    ("Bolivia", "BO"),
    ("Bosnia and Herzegovina", "BA"),
    ("Botswana", "BW"),
    ("Brazil", "BR"),
    ("Brunei", "BN"),
    ("Bulgaria", "BG"),
    ("Burkina Faso", "BF"),
    ("Burundi", "BI"),
    ("Cabo Verde", "CV"),
    ("Cambodia", "KH"),
    ("Cameroon", "CM"),
    ("Canada", "CA"),
    ("Central African Republic", "CF"),
    ("Chad", "TD"),
    ("Chile", "CL"),
    ("China", "CN"),
    ("Colombia", "CO"),
    ("Comoros", "KM"),
    ("Congo", "CG"),
    ("Costa Rica", "CR"),
    ("Cote d'Ivoire", "CI"),
    ("Croatia", "HR"),
    ("Cuba", "CU"),
    ("Cyprus", "CY"),
    ("Czechia", "CZ"),
    ("Democratic Republic of the Congo", "CD"),
    ("Denmark", "DK"),
    ("Djibouti", "DJ"),
    ("Dominica", "DM"),
    ("Dominican Republic", "DO"),
    ("Ecuador", "EC"),
    ("Egypt", "EG"),
    ("El Salvador", "SV"),
    ("Equatorial Guinea", "GQ"),
    ("Eritrea", "ER"),
    ("Estonia", "EE"),
    ("Eswatini", "SZ"),
    ("Ethiopia", "ET"),
    ("Fiji", "FJ"),
    ("Finland", "FI"),
    ("France", "FR"),
    ("Gabon", "GA"),
    ("Gambia", "GM"),
    ("Georgia", "GE"),
    ("Germany", "DE"),
    ("Ghana", "GH"),
    ("Greece", "GR"),
    ("Grenada", "GD"),
    ("Guatemala", "GT"),
    ("Guinea", "GN"),
    ("Guinea-Bissau", "GW"),
    ("Guyana", "GY"),
    ("Haiti", "HT"),
    ("Honduras", "HN"),
    ("Hungary", "HU"),
    ("Iceland", "IS"),
    ("India", "IN"),
    ("Indonesia", "ID"),
    ("Iran", "IR"),
    ("Iraq", "IQ"),
    ("Ireland", "IE"),
    ("Israel", "IL"),
    ("Italy", "IT"),
    ("Jamaica", "JM"),
    ("Japan", "JP"),
    ("Jordan", "JO"),
    ("Kazakhstan", "KZ"),
    ("Kenya", "KE"),
    ("Kiribati", "KI"),
    ("Kuwait", "KW"),
    ("Kyrgyzstan", "KG"),
    ("Laos", "LA"),
    ("Latvia", "LV"),
    ("Lebanon", "LB"),
    ("Lesotho", "LS"),
    ("Liberia", "LR"),
    ("Libya", "LY"),
    ("Liechtenstein", "LI"),
    ("Lithuania", "LT"),
    ("Luxembourg", "LU"),
    ("Madagascar", "MG"),
    ("Malawi", "MW"),
    ("Malaysia", "MY"),
    ("Maldives", "MV"),
    ("Mali", "ML"),
    ("Malta", "MT"),
    ("Marshall Islands", "MH"),
    ("Mauritania", "MR"),
    ("Mauritius", "MU"),
    ("Mexico", "MX"),
    ("Micronesia", "FM"),
    ("Moldova", "MD"),
    ("Monaco", "MC"),
    ("Mongolia", "MN"),
    ("Montenegro", "ME"),
    ("Morocco", "MA"),
    ("Mozambique", "MZ"),
    ("Myanmar", "MM"),
    ("Namibia", "NA"),
    ("Nauru", "NR"),
    ("Nepal", "NP"),
    ("Netherlands", "NL"),
    ("New Zealand", "NZ"),
    ("Nicaragua", "NI"),
    ("Niger", "NE"),
    ("Nigeria", "NG"),
    ("North Korea", "KP"),
    ("North Macedonia", "MK"),
    ("Norway", "NO"),
    ("Oman", "OM"),
    ("Pakistan", "PK"),
    ("Palau", "PW"),
    ("Palestine", "PS"),
    ("Panama", "PA"),
    ("Papua New Guinea", "PG"),
    ("Paraguay", "PY"),
    ("Peru", "PE"),
    ("Philippines", "PH"),
    ("Poland", "PL"),
    ("Portugal", "PT"),
    ("Qatar", "QA"),
    ("Romania", "RO"),
    ("Russia", "RU"),
    ("Rwanda", "RW"),
    ("Saint Kitts and Nevis", "KN"),
    ("Saint Lucia", "LC"),
    ("Saint Vincent and the Grenadines", "VC"),
    ("Samoa", "WS"),
    ("San Marino", "SM"),
    ("Sao Tome and Principe", "ST"),
    ("Saudi Arabia", "SA"),
    ("Senegal", "SN"),
    ("Serbia", "RS"),
    ("Seychelles", "SC"),
    ("Sierra Leone", "SL"),
    ("Singapore", "SG"),
    ("Slovakia", "SK"),
    ("Slovenia", "SI"),
    ("Solomon Islands", "SB"),
    ("Somalia", "SO"),
    ("South Africa", "ZA"),
    ("South Korea", "KR"),
    ("South Sudan", "SS"),
    ("Spain", "ES"),
    ("Sri Lanka", "LK"),
    ("Sudan", "SD"),
    ("Suriname", "SR"),
    ("Sweden", "SE"),
    ("Switzerland", "CH"),
    ("Syria", "SY"),
    ("Taiwan", "TW"),
    ("Tajikistan", "TJ"),
    ("Tanzania", "TZ"),
    ("Thailand", "TH"),
    ("Timor-Leste", "TL"),
    ("Togo", "TG"),
    ("Tonga", "TO"),
    ("Trinidad and Tobago", "TT"),
    ("Tunisia", "TN"),
    ("Turkey", "TR"),
    ("Turkmenistan", "TM"),
    ("Tuvalu", "TV"),
    ("Uganda", "UG"),
    ("Ukraine", "UA"),
    ("United Arab Emirates", "AE"),
    ("United Kingdom", "GB"),
    ("United States", "US"),
    ("Uruguay", "UY"),
    ("Uzbekistan", "UZ"),
    ("Vanuatu", "VU"),
    ("Vatican City", "VA"),
    ("Venezuela", "VE"),
    ("Vietnam", "VN"),
    ("Yemen", "YE"),
    ("Zambia", "ZM"),
    ("Zimbabwe", "ZW"),
    ("Aland Islands", "AX"),
    ("American Samoa", "AS"),
    ("Anguilla", "AI"),
    ("Antarctica", "AQ"),
    ("Aruba", "AW"),
    ("Bermuda", "BM"),
    ("Bouvet Island", "BV"),
    ("Caribbean Netherlands", "BQ"),
    ("Cocos (Keeling) Islands", "CC"),
    ("Cook Islands", "CK"),
    ("Curacao", "CW"),
    ("Christmas Island", "CX"),
    ("Western Sahara", "EH"),
    ("Falkland Islands", "FK"),
    ("Faroe Islands", "FO"),
    ("French Guiana", "GF"),
    ("Guernsey", "GG"),
    ("Gibraltar", "GI"),
    ("Greenland", "GL"),
    ("Guadeloupe", "GP"),
    ("South Georgia and the South Sandwich Islands", "GS"),
    ("Guam", "GU"),
    ("Hong Kong", "HK"),
    ("Heard Island and McDonald Islands", "HM"),
    ("Isle of Man", "IM"),
    ("British Indian Ocean Territory", "IO"),
    ("Jersey", "JE"),
    ("Cayman Islands", "KY"),
    ("Saint Barthelemy", "BL"),
    ("Saint Martin (French)", "MF"),
    ("Macau", "MO"),
    ("Northern Mariana Islands", "MP"),
    ("Martinique", "MQ"),
    ("Montserrat", "MS"),
    ("New Caledonia", "NC"),
    ("Norfolk Island", "NF"),
    ("Niue", "NU"),
    ("French Polynesia", "PF"),
    ("Saint Pierre and Miquelon", "PM"),
    ("Pitcairn", "PN"),
    ("Puerto Rico", "PR"),
    ("Reunion", "RE"),
    ("Saint Helena", "SH"),
    ("Svalbard and Jan Mayen", "SJ"),
    ("Sint Maarten (Dutch)", "SX"),
    ("Turks and Caicos Islands", "TC"),
    ("French Southern Territories", "TF"),
    ("Tokelau", "TK"),
    ("United States Minor Outlying Islands", "UM"),
    ("Virgin Islands (British)", "VG"),
    ("Virgin Islands (U.S.)", "VI"),
    ("Wallis and Futuna", "WF"),
    ("Mayotte", "YT"),
)
COUNTRY_CODES = frozenset(code for _name, code in COUNTRY_OPTIONS)

PREVIEW_WIFI_NETWORKS = [
    {"ssid": "FXRoute Studio", "signal": -42, "secured": True, "country": "DE"},
    {"ssid": "Home network", "signal": -57, "secured": True},
    {"ssid": "Open guest", "signal": -71, "secured": False},
]


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


def decode_iw_ssid(value: str) -> str:
    """Decode iw's escaped SSID bytes without trimming real SSID spaces."""

    if value.startswith(" "):
        value = value[1:]
    decoded = bytearray()
    index = 0
    while index < len(value):
        if (
            value[index : index + 2] == r"\x"
            and index + 4 <= len(value)
            and re.fullmatch(r"[0-9a-fA-F]{2}", value[index + 2 : index + 4])
        ):
            decoded.append(int(value[index + 2 : index + 4], 16))
            index += 4
            continue
        if value[index : index + 2] == r"\\":
            decoded.append(ord("\\"))
            index += 2
            continue
        decoded.extend(value[index].encode("utf-8"))
        index += 1
    return decoded.decode("utf-8", errors="replace")


def parse_wifi_scan(iw_output: str) -> list[dict[str, object]]:
    """Return visible Wi-Fi networks from ``iw dev <interface> scan`` output."""

    networks: dict[str, dict[str, object]] = {}
    current: dict[str, object] | None = None

    def commit() -> None:
        if current is None:
            return
        ssid = current.get("ssid")
        if not isinstance(ssid, str) or not ssid:
            return
        signal = current.get("signal")
        if not isinstance(signal, int):
            signal = None
        country = current.get("country")
        record = {
            "ssid": ssid,
            "signal": signal,
            "secured": bool(current.get("secured")),
        }
        if country:
            record["country"] = country
        previous = networks.get(ssid)
        if previous is None:
            networks[ssid] = record
            return
        previous["secured"] = bool(previous["secured"] or record["secured"])
        previous_signal = previous.get("signal")
        if previous_signal is None or (
            signal is not None and signal > previous_signal
        ):
            previous["signal"] = signal
        if not previous.get("country") and record.get("country"):
            previous["country"] = record["country"]

    for line in iw_output.splitlines():
        if re.match(
            r"^BSS [0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}(?:\s|\()", line
        ):
            commit()
            current = {}
            continue
        if current is None:
            continue
        field = line.strip()
        if field.startswith("SSID:"):
            current["ssid"] = decode_iw_ssid(
                line[line.index("SSID:") + len("SSID:") :]
            )
        elif field.startswith("signal:"):
            match = re.search(r"(-?\d+(?:\.\d+)?)\s+dBm", field)
            if match:
                current["signal"] = int(round(float(match.group(1))))
        elif field.startswith("Country:"):
            match = re.search(r"Country: ([A-Z]{2})", field)
            if match:
                current["country"] = match.group(1)
        elif field.startswith(("RSN:", "WPA:")):
            current["secured"] = True
        elif field.startswith("capability:") and "Privacy" in field:
            current["secured"] = True
    commit()

    return sorted(
        networks.values(),
        key=lambda network: (
            network["signal"] is None,
            -(
                network["signal"]
                if isinstance(network["signal"], int)
                else -1000
            ),
            str(network["ssid"]).casefold(),
        ),
    )


def scan_wifi_networks(interface: str) -> list[dict[str, object]]:
    if not interface:
        return []
    try:
        result = command(
            ["iw", "dev", interface, "scan"],
            check=False,
            timeout=WIFI_SCAN_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Wi-Fi scan timed out") from error
    if result.returncode != 0:
        raise RuntimeError("Wi-Fi scan failed")
    return parse_wifi_scan(result.stdout)


def hostname() -> str:
    try:
        value = Path("/etc/hostname").read_text(encoding="utf-8").strip()
    except OSError:
        value = "armbian"
    value = re.sub(r"[^A-Za-z0-9-]", "-", value).strip("-")
    return value or "armbian"


def onboarding_ssid(hostname_value: str) -> str:
    suffix = AP_SSID_SUFFIX
    prefix = re.sub(r"[^A-Za-z0-9-]", "-", hostname_value).strip("-")
    return f"{prefix[: 32 - len(suffix)] or 'armbian'}{suffix}"


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
    if country not in COUNTRY_CODES:
        raise ValueError("Choose a supported Wi-Fi country or region")
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
    if any(char in password for char in "\x00\r\n"):
        raise ValueError("Account password contains an invalid character")
    password_length = len(password)
    if password_length < 4:
        raise ValueError("Account password must be at least 4 characters")
    if password != password_confirmation:
        raise ValueError("Account passwords do not match")
    if ssh_key:
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
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=timeout,
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


def interface_ipv4_addresses(interface: str | None = None) -> list[str]:
    """Return global IPv4 addresses without the temporary setup AP address."""

    if interface:
        args = ["ip", "-4", "addr", "show", "dev", interface, "scope", "global"]
    else:
        args = ["ip", "-4", "addr", "show", "scope", "global"]
    result = command(args, check=False)
    if result.returncode != 0:
        return []
    addresses: list[str] = []
    for match in re.finditer(
        r"\binet\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)/", result.stdout
    ):
        address = match.group(1)
        if address == AP_ADDRESS or address in addresses:
            continue
        addresses.append(address)
    return addresses


MACHINE_ID_PATH = Path("/etc/machine-id")
FXROUTE_WEB_PORT = 8000


def derive_fxroute_device_name(
    machine_id_text: str | None = None,
    machine_id_path: Path = MACHINE_ID_PATH,
) -> str:
    """Mirror first-boot-install.sh derive_fxroute_device_name in Python."""

    if machine_id_text is None:
        try:
            if machine_id_path.is_symlink():
                return "fxroute"
            machine_id_text = machine_id_path.read_text(encoding="utf-8")
        except OSError:
            return "fxroute"
    cleaned = re.sub(r"[^a-z0-9]", "", machine_id_text.lower())
    if len(cleaned) >= 6:
        return f"fxroute-{cleaned[:6]}"
    return "fxroute"


def fxroute_lan_url(device_name: str) -> str:
    """Return the stable post-install web address for a device name."""

    return f"http://{device_name}.local:{FXROUTE_WEB_PORT}"


def completion_device_name(hostname_value: str, preview: bool = False) -> str:
    """Return the device name shown on the setup completion page."""

    if preview:
        cleaned = re.sub(r"[^A-Za-z0-9-]", "-", hostname_value).strip("-")
        return (cleaned or "fxroute-preview").lower()
    return derive_fxroute_device_name()


CONSOLE_PATHS = (Path("/dev/console"), Path("/dev/tty1"))


def ethernet_setup_console_lines(addresses: list[str]) -> list[str]:
    """Console lines pointing another computer at the LAN setup page."""

    urls = [f"https://{address}" for address in addresses]
    if not urls:
        return []
    return [
        "FXRoute setup: open "
        + (" or ".join(urls))
        + " on another computer to configure this device."
    ]


def ready_console_lines(addresses: list[str], device_name: str) -> list[str]:
    """Console lines for the finished installation with IP and .local."""

    lan_url = fxroute_lan_url(device_name)
    ip_urls = [f"http://{address}:{FXROUTE_WEB_PORT}" for address in addresses]
    if not ip_urls:
        return [f"FXRoute ready: {lan_url}"]
    return [f"FXRoute ready: {', '.join(ip_urls)} and {lan_url}"]


def announce_console(
    lines: list[str],
    console_paths: tuple[Path, ...] = CONSOLE_PATHS,
) -> None:
    """Mirror status lines to the attached monitor without ever failing."""

    if not lines:
        return
    for line in lines:
        LOG.info("%s", line)
    text = "\n".join(lines) + "\n"
    for console_path in console_paths:
        try:
            if console_path.is_symlink():
                continue
            with console_path.open("a", encoding="utf-8") as stream:
                stream.write(text)
        except OSError:
            continue


def setup_completion_html(
    device_name: str, addresses: list[str], username: str
) -> str:
    """Render the completion fragment with the prominent .local address."""

    url = fxroute_lan_url(device_name)
    escaped_url = html.escape(url, quote=True)
    escaped_text = html.escape(url)
    escaped_device = html.escape(f"{username}@{device_name}.local")
    parts = [
        "<h2>Setup complete</h2>",
        "<p>Your future FXRoute address:</p>",
        f'<p><strong><a href="{escaped_url}">{escaped_text}</a></strong></p>',
        f'<p><a href="{escaped_url}">Open FXRoute</a> '
        f'<button type="button" id="copy-address" '
        f'data-address="{escaped_url}">Copy address</button></p>',
        "<script>"
        "(function(){var button=document.getElementById('copy-address');"
        "if(!button)return;"
        "button.addEventListener('click',function(){"
        "var address=button.getAttribute('data-address');"
        "function done(){button.textContent='Copied';}"
        "if(navigator.clipboard&&navigator.clipboard.writeText){"
        "navigator.clipboard.writeText(address).then(done,function(){fallback();});"
        "}else{fallback();}"
        "function fallback(){"
        "var area=document.createElement('textarea');"
        "area.value=address;document.body.appendChild(area);area.select();"
        "try{document.execCommand('copy');done();}catch(e){}"
        "area.remove();"
        "}"
        "});})();"
        "</script>",
        "<p>FXRoute is being installed. The web interface will be "
        "available at the address above once first-boot installation "
        "finishes.</p>",
    ]
    if addresses:
        links = "".join(
            f'<li><a href="http://{html.escape(address)}:{FXROUTE_WEB_PORT}">'
            f"http://{html.escape(address)}:{FXROUTE_WEB_PORT}</a></li>"
            for address in addresses
        )
        parts.append(
            "<p>While the device keeps its current network address, "
            f"it is also reachable at:</p><ul>{links}</ul>".format(links=links)
        )
    else:
        parts.append(
            "<p>If the .local name does not resolve yet, find the device "
            "address on your router; FXRoute will be at "
            "http://&lt;address&gt;:8000.</p>"
        )
    parts.append(
        f"<p>SSH will be available as {escaped_device} once the device "
        f"is reachable on your normal network. If the temporary "
        f"setup network disappeared, reconnect your computer first.</p>"
        f"</body></html>"
    )
    return "".join(parts)


def preview_completion_html(device_name: str) -> str:
    """Render the preview completion fragment with the same .local UX."""

    url = fxroute_lan_url(device_name)
    escaped_url = html.escape(url, quote=True)
    escaped_text = html.escape(url)
    return (
        "<h2>Preview accepted</h2>"
        "<p>No system changes were made.</p>"
        "<p>Your future FXRoute address (preview):</p>"
        f'<p><strong><a href="{escaped_url}">{escaped_text}</a></strong></p>'
        f'<p><a href="{escaped_url}">Open FXRoute</a> '
        f'<button type="button" id="copy-address" '
        f'data-address="{escaped_url}">Copy address</button></p>'
        "<script>"
        "(function(){var button=document.getElementById('copy-address');"
        "if(!button)return;"
        "button.addEventListener('click',function(){"
        "var address=button.getAttribute('data-address');"
        "function done(){button.textContent='Copied';}"
        "if(navigator.clipboard&&navigator.clipboard.writeText){"
        "navigator.clipboard.writeText(address).then(done,function(){fallback();});"
        "}else{fallback();}"
        "function fallback(){"
        "var area=document.createElement('textarea');"
        "area.value=address;document.body.appendChild(area);area.select();"
        "try{document.execCommand('copy');done();}catch(e){}"
        "area.remove();"
        "}"
        "});})();"
        "</script>"
        "</body></html>"
    )


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

        if ssh_key:
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


def build_ssh_config(ssh_key: str) -> str:
    """Return the drop-in sshd config for the onboarding outcome."""
    has_key = bool(ssh_key.strip())
    password_value = "no" if has_key else "yes"
    kbd_value = "no" if has_key else "yes"
    return (
        f"PasswordAuthentication {password_value}\n"
        f"KbdInteractiveAuthentication {kbd_value}\n"
        "PermitRootLogin no\n"
        "PubkeyAuthentication yes\n"
    )


def configure_ssh_access(ssh_key: str) -> None:
    atomic_write(
        SSH_CONFIG_PATH,
        build_ssh_config(ssh_key),
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
    def __init__(self, interface: str, hostname_value: str, preview: bool = False):
        self.interface = interface
        self.hostname = hostname_value
        self.preview = preview
        self.ap_processes: list[subprocess.Popen[bytes]] = []
        self.ap_active = False
        self.server: ThreadingHTTPServer | None = None
        self.secure_server: ThreadingHTTPServer | None = None
        self.shutdown_event = threading.Event()
        self.configure_lock = threading.RLock()
        self.ethernet_loss_deadline: float | None = None
        self.wifi_scan_cache: list[dict[str, object]] = []
        self.wifi_scan_lock = threading.Lock()
        self.wifi_scan_started_at = 0.0

    def has_usable_ethernet(self) -> bool:
        return self.preview or has_usable_ethernet()

    def wifi_networks(self) -> list[dict[str, object]]:
        if self.preview:
            return [dict(network) for network in PREVIEW_WIFI_NETWORKS]
        cached_networks = lambda: [dict(network) for network in self.wifi_scan_cache]
        if not self.wifi_scan_lock.acquire(blocking=False):
            if self.wifi_scan_cache:
                return cached_networks()
            raise RuntimeError("Wi-Fi scan is busy")
        try:
            now = time.monotonic()
            if self.wifi_scan_started_at and now - self.wifi_scan_started_at < WIFI_SCAN_MIN_INTERVAL:
                if self.wifi_scan_cache:
                    return cached_networks()
                raise RuntimeError("Wi-Fi scan is being rate limited")
            self.wifi_scan_started_at = now
            try:
                networks = scan_wifi_networks(self.interface)
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                if not self.wifi_scan_cache:
                    raise
                return cached_networks()

            setup_ssid = onboarding_ssid(self.hostname)
            filtered_networks = [
                dict(network)
                for network in networks
                if network.get("ssid") != setup_ssid
            ]
            if filtered_networks or not (self.ap_active and self.wifi_scan_cache):
                self.wifi_scan_cache = filtered_networks
            return cached_networks()
        finally:
            self.wifi_scan_lock.release()

    def prime_wifi_scan(self) -> None:
        try:
            if self.interface and not self.preview:
                command(["ip", "link", "set", "dev", self.interface, "up"], check=False)
            self.wifi_networks()
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            LOG.info("initial Wi-Fi scan unavailable; manual network entry remains available")

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
                configure_ssh_access(ssh_key)
            except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
                if account_written and not previous:
                    ACCOUNT_FILE.unlink(missing_ok=True)
                if created:
                    command(["userdel", "--remove", username], check=False)
                return False, str(error)
            return True, "Account configured"

    def start_access_point(self) -> None:
        RUN_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        ssid = onboarding_ssid(self.hostname)
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
            + "auth_algs=1\n",
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
    ) -> tuple[bool, str]:
        with self.configure_lock:
            if CONFIGURED_MARKER.exists() and not self.preview:
                return False, "Setup has already been completed"
            try:
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
                elif not self.has_usable_ethernet():
                    raise ValueError("Choose a Wi-Fi network to continue")
            except ValueError as error:
                return False, str(error)

            if self.preview:
                return True, "Preview submission accepted"

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
                    self.prime_wifi_scan()
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
        if not should_start_access_point(ethernet, self.interface):
            if not ethernet:
                raise RuntimeError("No Wi-Fi interface is available for headless setup")
            self.prime_wifi_scan()
            LOG.info("wired Ethernet detected; serving setup over the DHCP network")
            try:
                setup_addresses = interface_ipv4_addresses()
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                setup_addresses = []
            announce_console(ethernet_setup_console_lines(setup_addresses))
        else:
            self.prime_wifi_scan()
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


def country_options_markup(selected: str = "GB") -> str:
    return "".join(
        f'<option value="{code}"{" selected" if code == selected else ""}>'
        f"{html.escape(name)} ({code})</option>"
        for name, code in COUNTRY_OPTIONS
    )


def setup_page(
    hostname_value: str,
    error: str = "",
    ethernet: bool = False,
    ap_active: bool = True,
    preview: bool = False,
) -> str:
    del hostname_value, ethernet
    escaped_error = ""
    if error:
        escaped_error = f'<p class="error" role="alert">{html.escape(error)}</p>'
    preview_value = "true" if preview else "false"
    form_action = (
        f"https://{AP_ADDRESS}/setup" if ap_active and not preview else "/setup"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Configure your FXRoute administrator account and network.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' fill='%2317232d'/%3E%3Cpath d='M8 16h16' stroke='%23b9472f' stroke-width='4'/%3E%3C/svg%3E">
<title>FXRoute setup</title>
<style>
:root {{
  color-scheme: light;
  --ink: #17232d;
  --muted: #53636b;
  --line: #849399;
  --paper: #eef2f1;
  --card: #ffffff;
  --signal: #b9472f;
  --signal-soft: #fff0eb;
  --teal: #126b78;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  min-width: 320px;
  background: var(--paper);
  color: var(--ink);
  font: 16px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
.shell {{ max-width: 720px; margin: 0 auto; padding: 28px 20px 56px; }}
.masthead {{
  align-items: center;
  border-bottom: 1px solid var(--line);
  display: flex;
  justify-content: space-between;
  padding-bottom: 18px;
}}
.brand {{ align-items: center; display: flex; font-weight: 800; letter-spacing: -.03em; }}
.brand-mark {{
  align-items: center;
  background: var(--ink);
  color: #fff;
  display: inline-flex;
  font: 700 12px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
  height: 28px;
  justify-content: center;
  margin-right: 9px;
  width: 28px;
}}
.eyebrow, .section-kicker {{
  color: var(--teal);
  font: 700 11px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: .12em;
  text-transform: uppercase;
}}
.intro {{ padding: 42px 4px 28px; }}
.intro h1 {{ font-size: clamp(2rem, 7vw, 3.2rem); letter-spacing: -.06em; line-height: 1; margin: 0 0 12px; }}
.intro p {{ color: var(--muted); margin: 0; max-width: 36rem; }}
.card {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 14px;
  box-shadow: 0 12px 30px rgba(23, 35, 45, .05);
  margin-top: 16px;
  padding: clamp(20px, 5vw, 30px);
}}
.section-kicker {{ margin: 0 0 5px; }}
.card h2 {{ font-size: 1.25rem; letter-spacing: -.025em; margin: 0; }}
.section-heading {{ align-items: end; display: flex; justify-content: space-between; gap: 16px; }}
.section-note, .field-note {{ color: var(--muted); font-size: .92rem; margin: 8px 0 22px; }}
.field-grid {{ display: grid; gap: 16px; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
.field {{ margin-top: 18px; }}
.field:first-child {{ margin-top: 0; }}
.field-grid > .field {{ margin-top: 0; }}
label {{ display: block; font-size: .9rem; font-weight: 700; margin-bottom: 7px; }}
.optional {{ color: var(--muted); font-weight: 500; }}
input, select, textarea {{
  appearance: none;
  background: #fbfcfc;
  border: 1px solid var(--line);
  border-radius: 8px;
  color: var(--ink);
  font: inherit;
  padding: 11px 12px;
  width: 100%;
}}
/* Password/Country: two fields on one row on desktop. The password side
   takes the remaining space; the country selector stays narrow. */
.field-grid.fields-password-country {{
  grid-template-columns: minmax(0, 1fr) 150px;
}}

select {{
  background-image: linear-gradient(45deg, transparent 50%, var(--muted) 50%), linear-gradient(135deg, var(--muted) 50%, transparent 50%);
  background-position: calc(100% - 17px) 52%, calc(100% - 12px) 52%;
  background-repeat: no-repeat;
  background-size: 5px 5px, 5px 5px;
  padding-right: 34px;
}}
textarea {{ min-height: 108px; resize: vertical; }}
textarea::placeholder {{ color: var(--muted); opacity: 1; }}
input:focus, select:focus, textarea:focus {{ border-color: var(--teal); box-shadow: 0 0 0 3px rgba(18, 107, 120, .16); outline: none; }}
button:focus-visible {{ box-shadow: 0 0 0 3px var(--paper), 0 0 0 6px var(--teal); outline: none; }}
button {{
  border: 0;
  border-radius: 8px;
  cursor: pointer;
  font-family: inherit;
  font-size: .9rem;
  font-weight: 700;
  line-height: 1.2;
  min-height: 44px;
  padding: 11px 14px;
}}
button:disabled {{ cursor: wait; opacity: .6; }}
.primary {{ background: var(--signal); color: #fff; width: 100%; }}
.secondary {{ background: var(--ink); color: #fff; white-space: nowrap; }}
.text-button {{ background: transparent; color: var(--teal); min-height: 44px; padding: 12px 0; text-align: left; }}
.network-list {{ display: grid; gap: 8px; margin: 0 0 10px; }}
.network-item {{ min-width: 0; }}
.network-option {{
  align-items: center;
  background: #fbfcfc;
  border: 1px solid var(--line);
  color: var(--ink);
  display: grid;
  gap: 12px;
  grid-template-columns: 1fr auto auto;
  text-align: left;
  width: 100%;
}}
.network-option:hover, .network-option[aria-pressed="true"] {{ border-color: var(--signal); background: var(--signal-soft); }}
.network-name {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.network-meta {{ color: var(--muted); font-size: .78rem; font-weight: 600; }}
.signal-bars {{ align-items: end; display: flex; gap: 2px; height: 17px; }}
.signal-bars i {{ background: var(--line); display: block; width: 4px; }}
.signal-bars i:nth-child(1) {{ height: 5px; }}
.signal-bars i:nth-child(2) {{ height: 9px; }}
.signal-bars i:nth-child(3) {{ height: 13px; }}
.signal-bars i:nth-child(4) {{ height: 17px; }}
.signal-bars i.active {{ background: var(--signal); }}
.network-state {{ color: var(--muted); font-size: .9rem; margin: 2px 0 10px; }}
.network-error {{ background: var(--signal-soft); border-left: 4px solid var(--signal); color: #8c2f1b; padding: 10px 12px; }}
.selection {{ color: var(--muted); font-size: .84rem; margin: 14px 0 0; }}
.advanced {{ border-top: 1px solid var(--line); margin-top: 24px; padding-top: 18px; }}
.advanced summary {{ align-items: center; cursor: pointer; display: flex; font-weight: 700; list-style: none; min-height: 44px; }}
.advanced summary:focus-visible {{ border-radius: 4px; box-shadow: 0 0 0 3px var(--paper), 0 0 0 6px var(--teal); outline: none; }}
.advanced summary::-webkit-details-marker {{ display: none; }}
.advanced summary::before {{ color: var(--signal); content: "+"; display: inline-block; font-size: 1.2rem; margin-right: 8px; vertical-align: -1px; }}
.advanced[open] summary::before {{ content: "-"; }}
.advanced-body {{ padding-top: 16px; }}
.error {{ background: #fff0eb; border-left: 4px solid var(--signal); color: #8c2f1b; margin: 0 0 16px; padding: 12px 14px; }}
@media (max-width: 560px) {{
  .shell {{ padding-left: 14px; padding-right: 14px; }}
  .field-grid {{ grid-template-columns: 1fr; }}
  .field-grid.fields-password-country {{ grid-template-columns: 1fr; }}
  .section-heading {{ align-items: start; flex-direction: column; }}
  .secondary {{ width: 100%; }}
  .network-option {{ gap: 8px; grid-template-columns: 1fr auto; }}
  .network-meta {{ grid-column: 1 / 2; grid-row: 2; }}
  .signal-bars {{ grid-column: 2; grid-row: 1 / 3; }}
}}
</style>
</head>
<body>
<main class="shell">
  <header class="masthead">
    <div class="brand"><span class="brand-mark">FX</span><span>FXRoute</span></div>
    <span class="eyebrow">Admin console</span>
  </header>
  <section class="intro">
    <h1>FXRoute</h1>
    <p>Configure your FXRoute administrator account and network.</p>
  </section>
  {escaped_error}
  <form id="setup-form" method="post" action="{form_action}" data-preview="{preview_value}">
    <section class="card">
      <p class="section-kicker">Account</p>
      <h2>Administrator</h2>
      <div class="field">
        <label for="username">Username</label>
        <input id="username" name="username" maxlength="32" pattern="[A-Za-z_][A-Za-z0-9_.-]*" autocomplete="username" required autofocus>
      </div>
      <div class="field-grid">
        <div class="field">
          <label for="account_password">Password</label>
          <input id="account_password" name="account_password" type="password" minlength="4" autocomplete="new-password" required>
        </div>
        <div class="field">
          <label for="account_password_confirm">Repeat password</label>
          <input id="account_password_confirm" name="account_password_confirm" type="password" minlength="4" autocomplete="new-password" required>
        </div>
      </div>
      <details class="advanced">
        <summary>Advanced / SSH</summary>
        <div class="advanced-body">
          <label for="ssh_key">SSH public key <span class="optional">(optional)</span></label>
          <textarea id="ssh_key" name="ssh_key" rows="4" autocomplete="off" placeholder="ssh-ed25519 AAAA..."></textarea>
        </div>
      </details>
    </section>
    <section class="card">
      <div class="section-heading">
        <div><p class="section-kicker">Network</p><h2>Choose a Wi-Fi network</h2></div>
        <button class="secondary" id="scan-wifi" type="button">Rescan</button>
      </div>
      <p class="section-note">Choose a nearby network, or enter its name manually.</p>
      <div id="wifi-networks" class="network-list" role="list" aria-live="polite">
        <p class="network-state" role="listitem">Loading networks...</p>
      </div>
      <button class="text-button" id="manual-toggle" type="button" aria-controls="manual-network" aria-expanded="false">Enter network manually</button>
      <div id="manual-network" hidden>
        <div class="field">
          <label for="wifi_ssid">Network name</label>
          <input id="wifi_ssid" name="wifi_ssid" maxlength="32" autocomplete="off">
        </div>
      </div>
      <p id="wifi-selection" class="selection">No network selected</p>
      <div class="field-grid fields-password-country">
        <div class="field">
          <label for="wifi_password">Wi-Fi password <span id="wifi-password-note" class="optional">(if needed)</span></label>
          <input id="wifi_password" name="wifi_password" type="password" autocomplete="off">
        </div>
        <div class="field">
          <label for="wifi_country">Country / Region</label>
          <select id="wifi_country" name="wifi_country">{country_options_markup()}</select>
        </div>
      </div>
    </section>
    <section class="card">
      <button class="primary" type="submit">Save settings</button>
    </section>
  </form>
</main>
<script>
(() => {{
  const form = document.getElementById("setup-form");
  const networkList = document.getElementById("wifi-networks");
  const scanButton = document.getElementById("scan-wifi");
  const manualToggle = document.getElementById("manual-toggle");
  const manualNetwork = document.getElementById("manual-network");
  const ssidInput = document.getElementById("wifi_ssid");
  const wifiPassword = document.getElementById("wifi_password");
  const wifiPasswordNote = document.getElementById("wifi-password-note");
  const countrySelect = document.getElementById("wifi_country");
  const selection = document.getElementById("wifi-selection");
  let selectedNetworkSecured = null;
  let lastSsid = "";
  let hasScanned = false;
  let lastScanResults = [];

  function stateMessage(message, alert = false) {{
    const state = document.createElement("p");
    state.className = alert ? "network-state network-error" : "network-state";
    state.setAttribute("role", "listitem");
    if (alert) state.setAttribute("aria-live", "assertive");
    state.textContent = message;
    return state;
  }}

  function signalLabel(signal) {{
    const value = Number(signal);
    if (!Number.isFinite(value)) return "Signal unavailable";
    if (value >= -55) return "Strong signal";
    if (value >= -70) return "Good signal";
    return "Weak signal";
  }}

  function syncNetworkFields() {{
    countrySelect.required = Boolean(ssidInput.value || wifiPassword.value);
    wifiPassword.required = selectedNetworkSecured === true;
    wifiPasswordNote.textContent = selectedNetworkSecured === true ? "(required)" : "(if needed)";
  }}

  function clearNetworkSelection() {{
    networkList.querySelectorAll(".network-option").forEach((item) => {{
      item.setAttribute("aria-pressed", "false");
    }});
  }}

  function applyNetworkCountry(network) {{
    // Reuse the regulatory code reported by the Wi-Fi scan. Only preselect
    // when the scan provides a usable code and the dropdown knows it;
    // otherwise keep the current (possibly manually chosen) value. The
    // country stays freely editable either way.
    const code = typeof network.country === "string" ? network.country.toUpperCase() : "";
    if (!/^[A-Z]{{2}}$/.test(code)) return;
    if (!countrySelect.querySelector('option[value="' + code + '"]')) return;
    countrySelect.value = code;
  }}

  function matchScannedNetwork(ssid) {{
    // Find a scanned AP for a manually typed SSID: exact match first, then a
    // trimmed match so stray leading/trailing spaces do not hide the AP.
    if (!ssid) return null;
    return lastScanResults.find((network) => network.ssid === ssid)
      || lastScanResults.find((network) =>
        typeof network.ssid === "string" && network.ssid.trim() === ssid.trim()
      )
      || null;
  }}

  function prefillCountryForSsid(ssid) {{
    // Preselect the matching AP's country unless the user already picked a
    // country by hand (any deviation from the form default).
    const network = matchScannedNetwork(ssid);
    if (!network) return;
    const defaultValue = countrySelect.options[0] ? countrySelect.options[0].value : "GB";
    if (countrySelect.value !== defaultValue) return;
    applyNetworkCountry(network);
  }}

  function clearMissingNetworkSelection() {{
    if (!ssidInput.readOnly || !ssidInput.value) return;
    ssidInput.value = "";
    ssidInput.readOnly = false;
    wifiPassword.value = "";
    selectedNetworkSecured = null;
    lastSsid = "";
    selection.textContent = "No network selected";
    // No network selected anymore: fall back to the form default.
    countrySelect.value = countrySelect.options[0] ? countrySelect.options[0].value : "GB";
    syncNetworkFields();
  }}

  function renderNetworks(networks) {{
    lastScanResults = Array.isArray(networks) ? networks : [];
    const selectedSsid = ssidInput.readOnly ? ssidInput.value : "";
    let selectedNetworkFound = !selectedSsid;
    networkList.replaceChildren();
    if (!Array.isArray(networks) || networks.length === 0) {{
      clearMissingNetworkSelection();
      networkList.appendChild(stateMessage("No networks found. Enter the name manually."));
      return;
    }}
    networks.forEach((network) => {{
      const option = document.createElement("button");
      option.className = "network-option";
      option.type = "button";
      const networkSsid = String(network.ssid || "");
      const isSelected = networkSsid === selectedSsid;
      option.setAttribute("aria-pressed", String(isSelected));
      if (isSelected) {{
        selectedNetworkFound = true;
        selectedNetworkSecured = Boolean(network.secured);
        if (!selectedNetworkSecured) wifiPassword.value = "";
      }}
      const name = document.createElement("span");
      name.className = "network-name";
      name.textContent = networkSsid;
      const meta = document.createElement("span");
      meta.className = "network-meta";
      meta.textContent = (network.secured ? "Secured" : "Open") + " | " + signalLabel(network.signal);
      const bars = document.createElement("span");
      bars.className = "signal-bars";
      [-75, -65, -55, -45].forEach((threshold) => {{
        const bar = document.createElement("i");
        if (network.signal !== null && Number(network.signal) >= threshold) bar.className = "active";
        bars.appendChild(bar);
      }});
      option.append(name, meta, bars);
      option.addEventListener("click", () => {{
        if (ssidInput.value !== networkSsid || !network.secured) wifiPassword.value = "";
        ssidInput.value = networkSsid;
        ssidInput.readOnly = true;
        lastSsid = networkSsid;
        selectedNetworkSecured = Boolean(network.secured);
        applyNetworkCountry(network);
        manualNetwork.hidden = true;
        manualToggle.setAttribute("aria-expanded", "false");
        manualToggle.textContent = "Enter network manually";
        selection.textContent = "Selected: " + ssidInput.value;
        clearNetworkSelection();
        option.setAttribute("aria-pressed", "true");
        syncNetworkFields();
      }});
      const item = document.createElement("div");
      item.setAttribute("role", "listitem");
      item.className = "network-item";
      item.appendChild(option);
      networkList.appendChild(item);
    }});
    if (selectedSsid && !selectedNetworkFound) clearMissingNetworkSelection();
    syncNetworkFields();
  }}

  async function scanNetworks() {{
    const previousNetworkItems = [...networkList.querySelectorAll(".network-item")];
    scanButton.disabled = true;
    scanButton.textContent = "Scanning...";
    networkList.replaceChildren(stateMessage("Looking for nearby networks..."));
    try {{
      const response = await fetch("/api/wifi/scan?ts=" + Date.now());
      const payload = await response.json();
      if (!response.ok) throw new Error("scan failed");
      renderNetworks(payload);
    }} catch (_error) {{
      networkList.replaceChildren(...previousNetworkItems);
      networkList.appendChild(stateMessage("Scan unavailable. Try again or enter the name manually.", true));
    }} finally {{
      hasScanned = true;
      scanButton.disabled = false;
      scanButton.textContent = hasScanned ? "Scan again" : "Rescan";
    }}
  }}

  manualToggle.addEventListener("click", () => {{
    const opening = manualNetwork.hidden;
    manualNetwork.hidden = !opening;
    manualToggle.setAttribute("aria-expanded", String(opening));
    manualToggle.textContent = opening ? "Hide manual entry" : "Enter network manually";
    if (opening) {{
      if (ssidInput.readOnly) {{
        ssidInput.value = "";
        wifiPassword.value = "";
      }}
      ssidInput.readOnly = false;
      selectedNetworkSecured = null;
      lastSsid = ssidInput.value;
      // Manual entry is not tied to a scanned AP, so the country returns to
      // the form default until the user picks one.
      countrySelect.value = countrySelect.options[0] ? countrySelect.options[0].value : "GB";
      selection.textContent = ssidInput.value ? "Selected: " + ssidInput.value : "Enter a network name";
      clearNetworkSelection();
      ssidInput.focus();
    }} else if (!ssidInput.value) selection.textContent = "No network selected";
    syncNetworkFields();
  }});
  ssidInput.addEventListener("input", () => {{
    if (ssidInput.value !== lastSsid) wifiPassword.value = "";
    lastSsid = ssidInput.value;
    selectedNetworkSecured = null;
    selection.textContent = ssidInput.value ? "Selected: " + ssidInput.value : "No network selected";
    prefillCountryForSsid(ssidInput.value);
    syncNetworkFields();
  }});
  wifiPassword.addEventListener("input", syncNetworkFields);
  scanButton.addEventListener("click", scanNetworks);
  scanNetworks();
}})();
</script>
</body>
</html>"""


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, onboarding: Onboarding, secure: bool = False):
        self.request_slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)
        self.onboarding = onboarding
        self.secure = secure

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


class SetupHandler(BaseHTTPRequestHandler):
    server: SetupServer

    def send_content(self, content: str, status: int = 200, content_type: str = "text/html") -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def preview_mode(self) -> bool:
        return bool(getattr(self.server.onboarding, "preview", False))

    def ethernet_available(self) -> bool:
        if self.preview_mode():
            return True
        checker = getattr(self.server.onboarding, "has_usable_ethernet", None)
        return checker() if checker is not None else has_usable_ethernet()

    def begin_setup_response(
        self, wifi_ssid: str = "", ap_active: bool = False
    ) -> None:
        self.close_connection = True
        self.send_response(202)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(
            b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            b'<meta name="viewport" content="width=device-width,initial-scale=1">'
            b"<title>Applying FXRoute setup</title></head><body>"
            b"<h1>Applying FXRoute setup</h1>"
            b"<p>The account and network are being configured. "
            b"This can take a minute. Do not turn off the device.</p>"
        )
        if ap_active:
            self.wfile.write(
                b"<p>The temporary setup network disappears during this step. "
                b"Reconnect your computer to your normal network when it goes away.</p>"
            )
            if wifi_ssid:
                self.wfile.write(
                    f"<p>Joining Wi-Fi network: {html.escape(wifi_ssid)}.</p>".encode(
                        "utf-8"
                    )
                )
        else:
            self.wfile.write(
                b"<p>The device stays reachable on its wired network address.</p>"
            )
        self.wfile.flush()

    def setup_completion_addresses(self) -> list[str]:
        try:
            return interface_ipv4_addresses()
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            return []

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
        preview = self.preview_mode()
        # Serve the form only over HTTPS so the browser certificate warning
        # appears on the initial GET before any credentials are typed. Serving
        # the AP page over plain HTTP and posting to HTTPS instead discards
        # the POST body at the warning, forcing users to fill the form twice.
        if not self.server.secure and not preview:
            self.redirect_to_tls()
            return
        path = urlsplit(self.path).path
        if path == "/api/wifi/scan":
            try:
                networks = self.server.onboarding.wifi_networks()
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                self.send_content(
                    json.dumps({"error": "Wi-Fi scan unavailable"}),
                    503,
                    "application/json",
                )
            else:
                self.send_content(
                    json.dumps(networks, ensure_ascii=True),
                    content_type="application/json",
                )
            return
        if path in ("/", "/generate_204", "/hotspot-detect.html", "/ncsi.txt"):
            self.send_content(
                setup_page(
                    self.server.onboarding.hostname,
                    ethernet=self.ethernet_available(),
                    ap_active=self.server.onboarding.ap_active,
                    preview=preview,
                )
            )
        else:
            self.send_content("Not found\n", 404, "text/plain")

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/setup":
            self.send_content("Not found\n", 404, "text/plain")
            return
        preview = self.preview_mode()
        if not self.server.secure and not preview:
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
            validate_account(username, account_password, account_password_confirm, ssh_key)
            if wifi_ssid or wifi_password:
                validate_setup(wifi_ssid, wifi_password, country)
            elif not self.ethernet_available():
                raise ValueError("Choose a Wi-Fi network to continue")
        except (ValueError, UnicodeDecodeError) as error:
            self.send_content(
                setup_page(
                    self.server.onboarding.hostname,
                    str(error),
                    ethernet=self.ethernet_available(),
                    ap_active=self.server.onboarding.ap_active,
                    preview=preview,
                ),
                400,
            )
            return

        self.begin_setup_response(
            wifi_ssid=wifi_ssid,
            ap_active=bool(getattr(self.server.onboarding, "ap_active", False)),
        )
        try:
            success, message = self.server.onboarding.submit(
                username,
                account_password,
                account_password_confirm,
                ssh_key,
                wifi_ssid,
                wifi_password,
                country,
            )
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            success, message = False, str(error)
        if success:
            if preview:
                device_name = completion_device_name(
                    str(getattr(self.server.onboarding, "hostname", "")),
                    preview=True,
                )
                self.wfile.write(
                    preview_completion_html(device_name).encode("utf-8")
                )
            else:
                # The Wi-Fi AP may already be gone, so stop both listeners before
                # attempting the optional final response write.
                self.server.onboarding.request_shutdown()
                device_name = completion_device_name(
                    str(getattr(self.server.onboarding, "hostname", ""))
                )
                addresses = self.setup_completion_addresses()
                self.wfile.write(
                    setup_completion_html(
                        device_name, addresses, username
                    ).encode("utf-8")
                )
            self.wfile.flush()
        else:
            LOG.warning("Wi-Fi setup was not completed: %s", message)
            self.wfile.write(
                f"<h2>Setup failed</h2><p>{html.escape(message)}</p>"
                f"</body></html>".encode("utf-8")
            )
            self.wfile.flush()

    def log_message(self, format: str, *args) -> None:
        LOG.info("%s - %s", self.address_string(), format % args)


def serve_preview(port: int) -> int:
    onboarding = Onboarding("preview-wlan0", "fxroute-preview", preview=True)
    server = SetupServer(("127.0.0.1", port), SetupHandler, onboarding)
    try:
        print(
            f"FXRoute browser preview: http://127.0.0.1:{server.server_port}",
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the FXRoute first-boot web setup")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="serve the production page locally without changing this machine",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="local preview port (default: 8765)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[armbian-web-config] %(message)s")
    if args.preview:
        return serve_preview(args.port)
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
