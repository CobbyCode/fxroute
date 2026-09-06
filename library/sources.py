"""Discovery and activation for local and mounted network music libraries."""

import ipaddress
import logging
import os
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse


logger = logging.getLogger(__name__)

_SYSTEM_SHARES = {"admin$", "ipc$", "print$", "profiles", "users"}

# Upper bound for parallel host probes during one discovery cycle. The
# per-command subprocess timeouts stay the actual bound; parallelism only
# stops one slow/dead host from serializing all others behind it.
_DISCOVERY_MAX_WORKERS = 8

# Minimum interval between two network discovery scans. This only throttles
# user-triggered rescans (opening the library selector, explicit refresh);
# it never starts a scan by itself, so an idle system performs no network
# probing at all.
_DISCOVERY_MIN_INTERVAL_SECONDS = 30.0

# Active subnet scan bounds for the automatic (unconfigured) discovery path.
# A fresh boot has an empty neighbor table, so discovery must not depend on
# already learned ARP entries: hosts with an open SMB port are found via a
# short parallel TCP 445 sweep of each local IPv4 network. Only networks up
# to _SUBNET_MAX_HOSTS hosts are swept; larger networks fall back to the
# neighbor table so a /16 never triggers a 65k-host scan.
_SMB_PORT = 445
_SMB_CONNECT_TIMEOUT_SECONDS = 0.4
_SUBNET_SCAN_MAX_WORKERS = 64
_SUBNET_MAX_HOSTS = 1024


def _valid_smb_name(value: str, *, allow_spaces: bool = False) -> bool:
    pattern = r"[A-Za-z0-9 ._()$-]+" if allow_spaces else r"[A-Za-z0-9._-]+"
    return bool(re.fullmatch(pattern, value or "")) and value not in {".", "..", "--remove-all"}


def _neighbor_hosts() -> list[str]:
    """IPv4 addresses from the kernel neighbor table (passive, may be empty)."""
    hosts: list[str] = []
    try:
        result = subprocess.run(
            ["ip", "neigh", "show"], capture_output=True, text=True, timeout=2, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return hosts
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if not parts:
            continue
        address = parts[0]
        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", address) and address not in hosts:
            hosts.append(address)
    return hosts


def _local_ipv4_networks() -> tuple[list["ipaddress.IPv4Network"], set[str]]:
    """Local IPv4 networks plus own addresses from `ip -o -4 addr show`."""
    networks: list["ipaddress.IPv4Network"] = []
    own: set[str] = set()
    try:
        result = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return networks, own
    if result.returncode != 0:
        return networks, own
    for match in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", result.stdout):
        address, prefix = match.group(1), int(match.group(2))
        own.add(address)
        try:
            network = ipaddress.IPv4Interface(f"{address}/{prefix}").network
        except ValueError:
            continue
        if network.is_loopback or network.prefixlen == 32:
            continue
        if network.num_addresses > _SUBNET_MAX_HOSTS + 2:
            continue
        if network not in networks:
            networks.append(network)
    return networks, own


def _smb_port_open(address: str, timeout: float = _SMB_CONNECT_TIMEOUT_SECONDS) -> bool:
    """Short TCP 445 probe (single host unit for the parallel subnet sweep)."""
    try:
        with socket.create_connection((address, _SMB_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _active_smb_hosts(
    timeout: float = _SMB_CONNECT_TIMEOUT_SECONDS,
    max_workers: int = _SUBNET_SCAN_MAX_WORKERS,
) -> list[str]:
    """Hosts with an open SMB port on the local IPv4 networks (active scan).

    Independent of the neighbor table, so a freshly booted host finds SMB
    servers without prior traffic. Candidates are probed in parallel with a
    short connect timeout, so dead/filtered addresses never serialize the
    cycle behind them.
    """
    networks, own = _local_ipv4_networks()
    candidates: list[str] = []
    seen: set[str] = set()
    for network in networks:
        for ip in network.hosts():
            candidate = str(ip)
            if candidate in own or candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)
    if not candidates:
        return []
    open_hosts: list[str] = []

    def _probe(address: str) -> bool:
        return _smb_port_open(address, timeout)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(candidates))) as pool:
        for address, is_open in zip(candidates, pool.map(_probe, candidates)):
            if is_open:
                open_hosts.append(address)
    return open_hosts


def default_discovery_hosts() -> list[str]:
    configured_hosts = os.environ.get("MUSIC_LIBRARY_SMB_HOSTS")
    raw_hosts = configured_hosts or ""
    hosts = [host.strip() for host in raw_hosts.split(",") if host.strip()]
    if configured_hosts:
        return hosts
    # Automatic path: passive neighbor entries plus an active TCP 445 sweep
    # of the local IPv4 networks. The sweep is what makes a fresh boot work
    # with an empty neighbor table; the neighbor table still contributes
    # hosts outside the swept networks (VPNs, other subnets).
    hosts = _neighbor_hosts()
    try:
        active = _active_smb_hosts()
    except Exception:
        active = []
    for address in active:
        if address not in hosts:
            hosts.append(address)
    return hosts


def _server_label(server: str) -> str:
    name = server.split(".", 1)[0]
    return name


def _smb_entry(server: str, share: str, display_server: str | None = None) -> dict[str, str]:
    server = server.strip()
    share = share.strip().strip("/")
    return {
        "id": f"smb:{server}:{share}",
        "type": "smb",
        "label": f"SMB — {display_server or _server_label(server)} / {share}",
        "server": server,
        "share": share,
    }


def _discover_host_shares(server: str) -> list[dict[str, str]]:
    """List guest-visible disk shares on one SMB host (single probe unit)."""
    shares: list[dict[str, str]] = []
    server = server.strip()
    if not server:
        return shares
    display_server, marker, address = server.partition("@")
    if marker:
        server = address
    else:
        display_server = _server_label(server)
    try:
        result = subprocess.run(
            ["smbclient", "-g", "-N", "-L", f"//{server}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return shares
    if result.returncode != 0:
        return shares
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        kind, separator, rest = line.partition("|")
        share, separator2, _comment = rest.partition("|")
        key = share.lower()
        if (
            kind != "Disk"
            or not separator
            or not separator2
            or not _valid_smb_name(server)
            or not _valid_smb_name(share, allow_spaces=True)
            or share.lower() in _SYSTEM_SHARES
            or key in seen
        ):
            continue
        try:
            access = subprocess.run(
                ["smbclient", "-N", f"//{server}/{share}", "-c", "ls"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if access.returncode != 0:
            continue
        seen.add(key)
        shares.append(_smb_entry(server, share, display_server))
    return shares


def discover_smb_shares(hosts: list[str]) -> list[dict[str, str]]:
    """List guest-visible disk shares on known SMB hosts.

    Hosts are probed in parallel with the usual bounded per-command
    timeouts, so one slow or unreachable host no longer serializes the
    whole cycle behind it. Results merge in host order (first host wins
    on duplicates), matching the previous sequential behavior.
    """
    servers = [server.strip() for server in hosts if server and server.strip()]
    if not servers:
        return []
    shares: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    with ThreadPoolExecutor(max_workers=min(_DISCOVERY_MAX_WORKERS, len(servers))) as pool:
        for server, host_shares in zip(servers, pool.map(_discover_host_shares, servers)):
            for entry in host_shares:
                key = (str(entry["server"]).lower(), str(entry["share"]).lower())
                if key in seen:
                    continue
                seen.add(key)
                shares.append(entry)
    return shares


class MusicLibraryManager:
    """Own the immutable local root and typed network library choices."""

    def __init__(
        self,
        local_root: Path,
        *,
        mount_root: Path | None = None,
        discovery_hosts: list[str] | None = None,
    ):
        self.local_root = local_root.expanduser().resolve(strict=False)
        self.mount_root = mount_root or (Path("/var/lib/fxroute/music-libraries") / str(os.getuid()))
        # An explicit host list (tests, operator override) stays frozen;
        # otherwise re-resolve on every discovery cycle (neighbor table plus
        # active local-subnet SMB sweep) so hosts appearing after service
        # start are found without a restart. Cycles only run on
        # user-triggered reads; there is no background timer.
        self._configured_hosts = discovery_hosts
        self.discovery_hosts = list(discovery_hosts) if discovery_hosts is not None else []
        self.active_id = "local"
        self.active_type = "local"
        self.active_root = self.local_root
        self._manual: dict[str, dict[str, str]] = {}
        self._discovered: list[dict[str, str]] = []
        self._discovered_at = 0.0

    def _resolve_discovery_hosts(self) -> list[str]:
        if self._configured_hosts is not None:
            return list(self._configured_hosts)
        return default_discovery_hosts()

    def list_libraries(self) -> list[dict[str, str]]:
        local = {"id": "local", "type": "local", "label": f"Local — {self.local_root.name or 'Music'}"}
        # Staleness gate only: rescans on user-triggered reads (selector /
        # settings opened, manual add, select) when the cache expired. There
        # is no background timer, so an idle system never scans; the frontend
        # likewise fetches only on dialog open and after selection.
        if time.monotonic() - self._discovered_at > _DISCOVERY_MIN_INTERVAL_SECONDS:
            hosts = self._resolve_discovery_hosts()
            self.discovery_hosts = list(hosts)
            logger.info("SMB discovery refresh: probing %d host(s)", len(hosts))
            self._discovered = discover_smb_shares(hosts)
            self._discovered_at = time.monotonic()
            logger.info("SMB discovery refresh: found %d share(s)", len(self._discovered))
        discovered = self._discovered
        merged = {entry["id"]: entry for entry in discovered}
        merged.update(self._manual)
        return [local, *merged.values(), {
            "id": "manual",
            "type": "action",
            "label": "Add network share manually…",
        }]

    def add_manual_url(self, url: str) -> dict[str, str]:
        parsed = urlparse(url.strip())
        if parsed.scheme.lower() != "smb" or not parsed.hostname:
            raise ValueError("Enter an SMB URL such as smb://server/share")
        share = unquote(parsed.path).strip("/")
        if not share or "/" in share:
            raise ValueError("The SMB URL must identify one share")
        return self.add_manual_share(parsed.hostname, share)

    def add_manual_share(self, server: str, share: str) -> dict[str, str]:
        if (
            not _valid_smb_name(server)
            or not _valid_smb_name(share, allow_spaces=True)
        ):
            raise ValueError("Invalid SMB server or share")
        entry = _smb_entry(server, share)
        self._manual[entry["id"]] = entry
        return entry

    def _mounted_share_path(self, server: str, share: str) -> Path | None:
        if not _valid_smb_name(server) or not _valid_smb_name(share, allow_spaces=True):
            return None
        configured = self.mount_root / server / share
        mount_root = self.mount_root.resolve(strict=False)
        if configured.resolve(strict=False).is_relative_to(mount_root) and configured.is_dir() and os.path.ismount(configured):
            return configured.resolve()
        gvfs = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "gvfs"
        for candidate in (
            gvfs / f"smb-share:server={server},share={share}",
            gvfs / f"smb-share:server={server.lower()},share={share.lower()}",
        ):
            if os.path.ismount(gvfs) and candidate.is_dir() and candidate.resolve(strict=False).is_relative_to(gvfs.resolve()):
                return candidate.resolve()
        return None

    def activate(self, library_id: str) -> Path:
        if library_id == "local":
            self.active_id = "local"
            self.active_type = "local"
            self.active_root = self.local_root
            return self.active_root
        if not library_id.startswith("smb:"):
            raise ValueError("Unsupported music library type")
        entries = {entry["id"]: entry for entry in self.list_libraries() if entry["type"] == "smb"}
        if library_id not in entries:
            raise ValueError("Unknown music library")
        _kind, server, share = library_id.split(":", 2)
        root = self._mounted_share_path(server, share)
        if root is None:
            try:
                subprocess.run(
                    ["sudo", "-n", "/usr/local/sbin/fxroute-cifs-mount", server, share],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            root = self._mounted_share_path(server, share)
        if root is None:
            try:
                subprocess.run(
                    ["gio", "mount", f"smb://{server}/{share}"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            root = self._mounted_share_path(server, share)
        if root is None:
            raise FileNotFoundError(f"SMB share is not mounted: {_server_label(server)} / {share}")
        self.active_id = library_id
        self.active_type = "smb"
        self.active_root = root
        return root

    def status(self) -> dict:
        return {
            "active_id": self.active_id,
            "active_type": self.active_type,
            "libraries": self.list_libraries(),
        }
