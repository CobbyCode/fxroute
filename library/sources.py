"""Discovery and activation for local and mounted network music libraries."""

import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from library import nfs


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
# short parallel TCP sweep of each local IPv4 network. Only networks up
# to _SUBNET_MAX_HOSTS hosts are swept; larger networks fall back to the
# neighbor table so a /16 never triggers a 65k-host scan.
_SMB_CONNECT_TIMEOUT_SECONDS = 0.4
_SUBNET_SCAN_MAX_WORKERS = 64
_SUBNET_MAX_HOSTS = 1024
# SMB ports probed by the active sweep. 445 is the modern direct-hosting
# port; 139 is kept so a hypothetical NetBIOS-only host is not silently
# dropped (smbclient itself would still reach it).
_SMB_PORTS = (445, 139)


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


def _smb_port_open(address: str, port: int, timeout: float = _SMB_CONNECT_TIMEOUT_SECONDS) -> bool:
    """Short TCP probe of one SMB port (single unit of the parallel sweep)."""
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def _address_in_networks(address: str, networks: list["ipaddress.IPv4Network"]) -> bool:
    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


def _sweep_open_hosts(
    networks: list["ipaddress.IPv4Network"],
    own: set[str],
    timeout: float = _SMB_CONNECT_TIMEOUT_SECONDS,
    max_workers: int = _SUBNET_SCAN_MAX_WORKERS,
    *,
    ports: tuple[int, ...] = _SMB_PORTS,
) -> list[str]:
    """Enumerated addresses with an open SMB port, in enumeration order.

    Independent of the neighbor table, so a freshly booted host finds SMB
    servers without prior traffic. Candidates are probed in parallel with a
    short connect timeout, so dead/filtered addresses never serialize the
    cycle behind them.
    """
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

    def _probe(pair: tuple[str, int]) -> str | None:
        address, port = pair
        return address if _smb_port_open(address, port, timeout) else None

    pairs = [(address, port) for address in candidates for port in ports]
    open_set: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(max_workers, len(pairs))) as pool:
        for address in pool.map(_probe, pairs):
            if address is not None:
                open_set.add(address)
    return [address for address in candidates if address in open_set]


def default_discovery_hosts(*, protocol: str = "smb") -> list[str]:
    configured_hosts = os.environ.get(f"MUSIC_LIBRARY_{protocol.upper()}_HOSTS")
    raw_hosts = configured_hosts or ""
    hosts = [host.strip() for host in raw_hosts.split(",") if host.strip()]
    if configured_hosts:
        return hosts
    # Automatic path: passive neighbor entries plus an active TCP sweep of
    # the local IPv4 networks. The sweep makes a fresh boot work with an
    # empty neighbor table; its verdicts also spare smbclient probes to
    # swept hosts with verifiably closed SMB ports (measured ~3.4 s of
    # wasted smbclient latency per dead neighbor on a busy /24, same
    # negative result). Hosts outside the swept networks cannot be judged
    # and are still probed, as is every host with an open SMB port.
    neigh = _neighbor_hosts()
    networks, _own = _local_ipv4_networks()
    try:
        if protocol == "nfs":
            open_hosts = _sweep_open_hosts(networks, _own, ports=(2049, 111)) if networks else []
        else:
            open_hosts = _sweep_open_hosts(networks, _own) if networks else []
    except Exception:
        open_hosts = []
    open_set = set(open_hosts)
    hosts = []
    for address in neigh:
        if address not in open_set and _address_in_networks(address, networks):
            continue
        if address not in hosts:
            hosts.append(address)
    for address in open_hosts:
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


@dataclass(frozen=True)
class LibraryContext:
    """Identity of the music-library context a piece of work was prepared for.

    ``generation`` is the decisive field: it advances once per committed
    library switch, so any work that captured an older generation (a running
    scan, a prepared playback transition) can be rejected before it publishes.
    """

    library_id: str
    library_type: str
    root: str
    generation: int


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
        state_path: Path | None = None,
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
        # Single-flight for network rescans: overlapping user-triggered
        # reads share one running scan instead of stampeding the LAN with
        # parallel subnet/smbclient sweeps. A plain thread lock is correct
        # here because rescans always run in worker threads (never awaited
        # while held) and the manager instance is never copied.
        self._discovery_lock = threading.Lock()
        # Context generation of the active library.  A committed switch
        # advances it exactly once, which is enough to invalidate every
        # capture taken before the switch.
        self._generation = 0
        self._generation_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self.state_path = state_path
        self.saved_active_id = "local"
        self.restore_error = ""
        self._hidden: set[str] = set()
        self._removing: set[str] = set()
        self._mounting: dict[str, int] = {}
        self._gvfs_paths: dict[str, Path] = {}
        self._owned_gvfs: set[str] = set()
        self._load_state()

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
            if not isinstance(data, dict):
                raise ValueError("Invalid music library state")
            for saved in data.get("libraries", []):
                try:
                    if saved["type"] == "nfs":
                        entry = nfs.nfs_entry(saved["server"], saved["share"])
                    elif saved["type"] == "smb":
                        if not _valid_smb_name(saved["server"]) or not _valid_smb_name(saved["share"], allow_spaces=True):
                            continue
                        entry = _smb_entry(saved["server"], saved["share"])
                    else:
                        continue
                    self._manual[entry["id"]] = entry
                except (KeyError, TypeError, ValueError):
                    continue
            self._hidden = {value for value in data.get("hidden", []) if isinstance(value, str)}
            selected = data.get("active_id", "local")
            if selected in self._manual:
                self.saved_active_id = selected
        except (OSError, ValueError, TypeError):
            logger.warning("Could not load music library state", exc_info=True)

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"active_id": self.saved_active_id, "libraries": list(self._manual.values()), "hidden": sorted(self._hidden)}
        fd, temporary = tempfile.mkstemp(prefix=".music-libraries-", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(data, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.state_path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _remember_selection(self, library_id: str, entry: dict | None = None, *, clear_restore_error: bool = True) -> None:
        with self._state_lock:
            if entry is not None:
                self._manual[library_id] = entry
                self._hidden.discard(library_id)
            self.saved_active_id = library_id
            self._save_state()
            if clear_restore_error:
                self.restore_error = ""

    # ── Library context generation ───────────────────────────────────

    def capture_context(self) -> LibraryContext:
        """Snapshot the active library context for work about to start."""
        with self._generation_lock:
            return LibraryContext(
                library_id=self.active_id,
                library_type=self.active_type,
                root=str(self.active_root),
                generation=self._generation,
            )

    def context_is_current(self, captured: LibraryContext | None) -> bool:
        """Whether no library switch committed since ``captured`` was taken."""
        if not isinstance(captured, LibraryContext):
            return False
        with self._generation_lock:
            return captured.generation == self._generation

    def active_snapshot(self) -> tuple[str, str, Path]:
        """The published active library, for a switch that may be rolled back."""
        with self._generation_lock:
            return (self.active_id, self.active_type, self.active_root)

    def restore_active(self, library_id: str, library_type: str, root: Path, *, remember_selection: bool = True) -> None:
        """Re-publish a previously active library without re-resolving it.

        Used to roll a failed switch back so the manager, scanner, queue and
        player describe one library again.  The generation is not advanced: a
        switch that failed never published a context, so no prepared work
        observed a new one and nothing has to be invalidated.
        """
        with self._generation_lock:
            self.active_id = library_id
            self.active_type = library_type
            self.active_root = root
        if remember_selection:
            self._remember_selection(library_id, clear_restore_error=False)

    def _resolve_discovery_hosts(self) -> list[str]:
        if self._configured_hosts is not None:
            return list(self._configured_hosts)
        return default_discovery_hosts()

    def _is_stale(self) -> bool:
        return time.monotonic() - self._discovered_at > _DISCOVERY_MIN_INTERVAL_SECONDS

    def discovery_running(self) -> bool:
        """Whether a network rescan is currently running (single-flight)."""
        return self._discovery_lock.locked()

    def claim_background_refresh(self, *, force: bool = False) -> bool:
        """Non-blocking single-flight claim for a background rescan.

        Returns True only to the one caller that must now run
        run_claimed_refresh() in a worker thread. Returns False when the
        cache is fresh (unless forced) or another scan is already running;
        waiting callers keep serving cached data and check back later.
        """
        if not force and not self._is_stale():
            return False
        if not self._discovery_lock.acquire(blocking=False):
            return False
        if not force and not self._is_stale():
            self._discovery_lock.release()
            return False
        return True

    def run_claimed_refresh(self) -> None:
        """Execute a claimed background rescan and release the claim.

        Must run in a worker thread; never blocks the event loop. Failures
        inside the scan keep the previous cache (discover_smb_shares only
        returns what it could verify).
        """
        try:
            self._run_discovery_scan()
        finally:
            self._discovery_lock.release()

    def _run_discovery_scan(self) -> None:
        hosts = self._resolve_discovery_hosts()
        self.discovery_hosts = list(hosts)
        nfs_hosts = list(self._configured_hosts) if self._configured_hosts is not None else default_discovery_hosts(protocol="nfs")
        logger.info("Network discovery refresh: probing %d SMB / %d NFS host(s)", len(hosts), len(nfs_hosts))
        with ThreadPoolExecutor(max_workers=2) as pool:
            smb_result = pool.submit(discover_smb_shares, hosts)
            nfs_result = pool.submit(nfs.discover_nfs_shares, nfs_hosts)
            self._discovered = [*smb_result.result(), *nfs_result.result()]
        self._discovered_at = time.monotonic()
        logger.info("Network discovery refresh: found %d share(s)", len(self._discovered))

    def _list_cached(self) -> list[dict[str, str]]:
        """Assemble the library list from known entries without any I/O."""
        local = {"id": "local", "type": "local", "label": f"Local — {self.local_root.name or 'Music'}"}
        with self._state_lock:
            merged = {entry["id"]: entry for entry in self._discovered if entry["id"] not in self._hidden}
            merged.update(self._manual)
        return [local, *merged.values(), {
            "id": "manual",
            "type": "action",
            "label": "Add network share manually…",
        }]

    def list_libraries(self) -> list[dict[str, str]]:
        # Staleness gate only: rescans on user-triggered reads (selector /
        # settings opened, manual add) when the cache expired. There
        # is no background timer, so an idle system never scans; the frontend
        # likewise fetches only on dialog open and after selection.
        # Overlapping reads share one running scan via single-flight.
        if self._is_stale():
            with self._discovery_lock:
                # Re-check after acquiring: a concurrent read may have
                # refreshed the cache while this caller was waiting.
                if self._is_stale():
                    self._run_discovery_scan()
        return self._list_cached()

    def add_manual_url(self, url: str) -> dict[str, str]:
        if not url.strip().lower().startswith("smb://"):
            entry = nfs.parse_nfs_url(url.strip())
            with self._state_lock:
                self._manual[entry["id"]] = entry
                self._hidden.discard(entry["id"])
                self._save_state()
            return entry
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
        with self._state_lock:
            self._manual[entry["id"]] = entry
            self._hidden.discard(entry["id"])
            self._save_state()
        return entry

    def remove_library(self, library_id: str) -> None:
        with self._state_lock:
            if library_id in {"local", self.active_id}:
                raise ValueError("Select the local library before removing this share")
            entry = self._network_entry(library_id)
            self._removing.add(library_id)
        try:
            self._remove_mount(entry)
            with self._state_lock:
                self._manual.pop(library_id, None)
                self._hidden.add(library_id)
                if not self._mounting.get(library_id):
                    self._gvfs_paths.pop(library_id, None)
                    self._owned_gvfs.discard(library_id)
                if self.saved_active_id == library_id:
                    self.saved_active_id = "local"
                    self.restore_error = ""
                self._save_state()
        finally:
            with self._state_lock:
                self._removing.discard(library_id)

    def _remove_mount(self, entry: dict[str, str]) -> None:
        library_id = entry['id']
        try:
            result = subprocess.run(
                ["sudo", "-n", nfs.MOUNT_HELPER, "--remove", entry["type"], entry["server"], entry["share"]],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Could not remove network mount") from exc
        if result.returncode:
            raise ValueError(result.stderr.strip() or "Could not remove network mount")
        with self._state_lock:
            owned_gvfs = library_id in self._owned_gvfs
            self._owned_gvfs.discard(library_id)
        if owned_gvfs:
            try:
                result = subprocess.run(
                    ["gio", "mount", "-u", f"smb://{entry['server']}/{entry['share']}"],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False,
                )
                if result.returncode:
                    raise ValueError(result.stderr.strip() or "Could not remove GVFS mount")
            except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
                with self._state_lock:
                    self._owned_gvfs.add(library_id)
                raise ValueError("Could not remove GVFS mount") from exc

    def _mounted_nfs_path(self, server: str, export: str) -> Path | None:
        return nfs.mounted_path(self.mount_root, server, export)

    def _mount_nfs(self, entry: dict[str, str]) -> Path:
        server, export = entry["server"], entry["share"]
        root = self._mounted_nfs_path(server, export)
        detail = ""
        if root is None:
            try:
                result = subprocess.run(
                    ["sudo", "-n", nfs.MOUNT_HELPER, "--nfs", server, export],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=False,
                )
                detail = result.stderr.strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                detail = str(exc)
            root = self._mounted_nfs_path(server, export)
        if root is None:
            raise FileNotFoundError(f"NFS share is not mounted: {server}:{export} ({detail or 'mount unavailable'})")
        return root

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

    def _publish_active(self, library_id: str, library_type: str, root: Path) -> int:
        """Publish the active library and advance the context generation.

        The generation only advances on a real change, so re-selecting the
        already active library leaves work prepared against it valid.
        """
        with self._generation_lock:
            changed = (
                self.active_id != library_id
                or self.active_type != library_type
                or self.active_root != root
            )
            self.active_id = library_id
            self.active_type = library_type
            self.active_root = root
            if changed:
                self._generation += 1
            return self._generation

    def _network_entry(self, library_id: str) -> dict[str, str]:
        if not library_id.startswith(("smb:", "nfs:")):
            raise ValueError("Unsupported music library type")
        entries = {entry["id"]: entry for entry in self._list_cached() if entry["type"] in {"smb", "nfs"}}
        if library_id not in entries or library_id in self._removing:
            raise ValueError("Unknown music library")
        return entries[library_id]

    def mount(self, library_id: str) -> Path:
        """Mount a network library and return its root without publishing it.

        The mount can take tens of seconds. A caller that holds a switch lock
        mounts first without it: ``activate`` then finds the share mounted and
        only publishes it.
        """
        with self._state_lock:
            entry = self._network_entry(library_id)
            self._mounting[library_id] = self._mounting.get(library_id, 0) + 1
        try:
            root = self._mount(entry)
            try:
                with self._state_lock:
                    self._network_entry(library_id)
            except ValueError as error:
                try:
                    self._remove_mount(entry)
                except Exception as cleanup_error:
                    logger.exception('Could not clean up a removed library mount')
                    error.add_note(f'Mount cleanup also failed: {cleanup_error}')
                raise
            return root
        finally:
            with self._state_lock:
                self._mounting[library_id] -= 1
                if not self._mounting[library_id]:
                    self._mounting.pop(library_id)
                    if library_id in self._hidden:
                        self._gvfs_paths.pop(library_id, None)

    def _mount(self, entry: dict[str, str]) -> Path:
        return self._mount_nfs(entry) if entry["type"] == "nfs" else self._mount_smb(entry)

    def activate(self, library_id: str, *, mount_if_needed: bool = True, clear_restore_error: bool = True) -> Path:
        if library_id == "local":
            self._remember_selection("local", clear_restore_error=clear_restore_error)
            self._publish_active("local", "local", self.local_root)
            return self.active_root
        entry = self._network_entry(library_id)
        if mount_if_needed:
            root = self.mount(library_id)
        else:
            root = (self._mounted_nfs_path(entry['server'], entry['share']) if entry['type'] == 'nfs'
                    else self._mounted_share_path(entry['server'], entry['share']))
            if root is None:
                raise FileNotFoundError('Music library mount disappeared before activation')
        with self._state_lock:
            entry = self._network_entry(library_id)
            self._remember_selection(library_id, entry, clear_restore_error=clear_restore_error)
            self._publish_active(library_id, entry["type"], root)
        return root

    def _mount_smb(self, entry: dict[str, str]) -> Path:
        library_id = entry["id"]
        _kind, server, share = library_id.split(":", 2)
        root = self._mounted_share_path(server, share)
        mount_diagnostics: list[str] = []
        mounted_by_gio = False

        def _note_diagnostics(result: subprocess.CompletedProcess | None, tool: str, timeout_s: int) -> None:
            if result is None:
                mount_diagnostics.append(f"{tool} timed out after {timeout_s}s")
                return
            error_lines = (result.stderr or "").strip().splitlines()
            if error_lines and error_lines[-1].strip():
                mount_diagnostics.append(error_lines[-1].strip())
            elif result.returncode != 0:
                mount_diagnostics.append(f"{tool} failed (exit {result.returncode})")

        if root is None:
            try:
                helper_result = subprocess.run(
                    ["sudo", "-n", "/usr/local/sbin/fxroute-cifs-mount", server, share],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                _note_diagnostics(None, "cifs-mount helper", 15)
            else:
                _note_diagnostics(helper_result, "cifs-mount helper", 15)
            root = self._mounted_share_path(server, share)
        if root is None:
            try:
                gio_result = subprocess.run(
                    ["gio", "mount", f"smb://{server}/{share}"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                _note_diagnostics(None, "gio mount", 10)
            else:
                _note_diagnostics(gio_result, "gio mount", 10)
                mounted_by_gio = gio_result.returncode == 0
            root = self._mounted_share_path(server, share)
        if root is None:
            detail = f"SMB share is not mounted: {_server_label(server)} / {share}"
            if mount_diagnostics:
                detail += f" ({'; '.join(mount_diagnostics)})"
                logger.warning("SMB mount failed for %s / %s: %s", server, share, "; ".join(mount_diagnostics))
            raise FileNotFoundError(detail)
        gvfs = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "gvfs"
        if root.is_relative_to(gvfs):
            with self._state_lock:
                self._gvfs_paths[library_id] = root
                if mounted_by_gio:
                    self._owned_gvfs.add(library_id)
        return root

    def status(self) -> dict:
        self.list_libraries()
        return self.status_cached()

    def status_cached(self) -> dict:
        """Select-path response: known entries only, never a network rescan."""
        mounts = nfs.read_mounts()
        libraries = []
        for entry in self._list_cached():
            if entry["type"] in {"smb", "nfs"}:
                path = (nfs.mount_path(self.mount_root, entry["server"], entry["share"]) if entry["type"] == "nfs"
                        else self.mount_root / entry["server"] / entry["share"])
                kind, _source = mounts.get(str(path), ("", ""))
                gvfs_path = self._gvfs_paths.get(entry["id"])
                gvfs_kind = mounts.get(str(gvfs_path.parent), ("", ""))[0] if gvfs_path else ""
                entry = {**entry, "mounted": kind in {"cifs", "nfs", "nfs4"} or gvfs_kind.startswith("fuse")}
            libraries.append(entry)
        return {
            "active_id": self.active_id,
            "active_type": self.active_type,
            "libraries": libraries,
            "saved_active_id": self.saved_active_id,
            "restore_error": self.restore_error,
        }
