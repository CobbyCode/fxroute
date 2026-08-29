"""Central server-side fetch boundary with public-internet target validation.

Every FXRoute-internal HTTP(S) fetch of a URL that is influenced by API
input goes through :func:`safe_get`.  Targets must use only http:// or
https://, must carry no userinfo, and must resolve to publicly routable
addresses.  Every redirect hop is re-validated with the same rules before
it is followed, so a redirect cannot bypass the initial check.

Response bodies are read with a counted, chunked read bounded by a
per-content-class limit, so a malicious or broken server cannot make the
process load an unbounded body into memory.

IPv4, IPv6 (including IPv4-mapped IPv6), NAT64 and 6to4 embedded IPv4
addresses are all checked.  An address must actually be globally routable
(ipaddress ``is_global`` semantics), with the conservative deny lists
below applied on top; loopback, RFC1918/ULA private ranges, link-local,
multicast, unspecified, documentation/TEST-NET, benchmarking, CGNAT,
Teredo, local-use NAT64 and reserved/broadcast targets are rejected by
default.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from requests.adapters import HTTPAdapter

DEFAULT_MAX_REDIRECTS = 5
DEFAULT_FETCH_READ_CHUNK_BYTES = 64 * 1024

# Named, generous per-content-class response-body limits (legit responses
# stay far below; the limits only stop unbounded body loading).
RADIO_PLAYLIST_FETCH_MAX_BYTES = 1 * 1024 * 1024   # .pls / .m3u / .m3u8 playlist resolution
SOMAFM_PAGE_FETCH_MAX_BYTES = 2 * 1024 * 1024      # somafm.com metadata page (HTML)
SOMAFM_ARTWORK_FETCH_MAX_BYTES = 8 * 1024 * 1024   # station artwork image
ENRICHMENT_JSON_MAX_BYTES = 4 * 1024 * 1024        # MusicBrainz / Wikidata / Wikipedia JSON
RADIO_BROWSER_FETCH_MAX_BYTES = 4 * 1024 * 1024    # radio-browser station search JSON
COVER_ART_MAX_BYTES = 8 * 1024 * 1024              # CoverArtArchive cover image

_PRIVATE_V4_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),        # "this network"
    ipaddress.ip_network("10.0.0.0/8"),       # RFC 1918
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("169.254.0.0/16"),   # link-local
    ipaddress.ip_network("172.16.0.0/12"),    # RFC 1918
    ipaddress.ip_network("192.0.0.0/24"),     # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),     # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),   # RFC 1918
    ipaddress.ip_network("198.18.0.0/15"),    # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),   # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),      # multicast
    ipaddress.ip_network("240.0.0.0/4"),      # reserved / broadcast
]

_PRIVATE_V6_NETWORKS = [
    ipaddress.ip_network("::/128"),           # unspecified
    ipaddress.ip_network("::1/128"),          # loopback
    ipaddress.ip_network("64:ff9b:1::/48"),   # NAT64 local-use (RFC 8215)
    ipaddress.ip_network("100::/64"),         # discard-only
    ipaddress.ip_network("2001::/32"),        # Teredo
    ipaddress.ip_network("2001:2::/48"),      # Benchmarking
    ipaddress.ip_network("2001:10::/28"),     # ORCHID
    ipaddress.ip_network("2001:db8::/32"),    # documentation
    ipaddress.ip_network("fc00::/7"),         # ULA
    ipaddress.ip_network("fe80::/10"),        # link-local
    ipaddress.ip_network("ff00::/8"),         # multicast
]

# IPv4-mapped (::ffff:0:0/96), well-known NAT64 (64:ff9b::/96) and 6to4
# (2002::/16) addresses are judged by their embedded IPv4 address in
# _ipv6_embedded_ipv4, so they are intentionally not listed as
# blanket-denied prefixes.

_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR_PREFIX = ipaddress.ip_network("2002::/16")


class BlockedUrlError(ValueError):
    """URL rejected by the public-internet fetch policy."""


class ResponseTooLargeError(ValueError):
    """Server response body exceeded the configured fetch limit."""


def _ipv4_is_public(address: ipaddress.IPv4Address) -> bool:
    return address.is_global and not any(address in network for network in _PRIVATE_V4_NETWORKS)


def _ipv6_embedded_ipv4(address: ipaddress.IPv6Address) -> Optional[ipaddress.IPv4Address]:
    if address.ipv4_mapped:
        return address.ipv4_mapped
    if address in _NAT64_PREFIX:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if address in _SIX_TO_FOUR_PREFIX:
        return ipaddress.IPv4Address((int(address) >> 80) & 0xFFFFFFFF)
    return None


def _ipv6_is_public(address: ipaddress.IPv6Address) -> bool:
    embedded = _ipv6_embedded_ipv4(address)
    if embedded is not None:
        # IPv4-mapped / NAT64 / 6to4 addresses are judged by their
        # embedded IPv4 address, so a formally global prefix with an
        # embedded 127/8, RFC1918 etc. stays blocked.
        return _ipv4_is_public(embedded)
    return address.is_global and not any(address in network for network in _PRIVATE_V6_NETWORKS)


def _literal_address(host: str) -> Optional[ipaddress._BaseAddress]:
    zone = host.split("%", 1)[0]
    try:
        return ipaddress.ip_address(zone)
    except ValueError:
        return None


def _validate_host(host: str) -> None:
    literal = _literal_address(host)
    if literal is not None:
        if isinstance(literal, ipaddress.IPv4Address):
            if not _ipv4_is_public(literal):
                raise BlockedUrlError(f"URL host {host!r} resolves to a non-public address")
            return
        if not _ipv6_is_public(literal):
            raise BlockedUrlError(f"URL host {host!r} resolves to a non-public address")
        return
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedUrlError(f"URL host {host!r} could not be resolved") from exc
    for family, _socktype, _proto, _canonname, sockaddr in addresses:
        if family == socket.AF_INET:
            address = ipaddress.IPv4Address(sockaddr[0])
            if not _ipv4_is_public(address):
                raise BlockedUrlError(f"URL host {host!r} resolves to a non-public address")
        elif family == socket.AF_INET6:
            address = ipaddress.IPv6Address(sockaddr[0].split("%", 1)[0])
            if not _ipv6_is_public(address):
                raise BlockedUrlError(f"URL host {host!r} resolves to a non-public address")


def validate_public_url(url: str) -> str:
    """Validate that ``url`` is a fetchable public http(s) target.

    Checks the scheme, rejects userinfo, and validates the host either as
    a literal IP or via DNS resolution of every returned address.  Raises
    :class:`BlockedUrlError` (a ``ValueError``) for non-http(s) schemes,
    userinfo, unresolvable hosts or non-public targets.
    """
    value = (url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise BlockedUrlError(f"Only http:// or https:// URLs are supported, got {parsed.scheme!r}")
    if parsed.username is not None or parsed.password is not None:
        raise BlockedUrlError("URLs with userinfo (user:pass@host) are not supported")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise BlockedUrlError("URL host is missing")
    _validate_host(host)
    return value


def _read_bounded(response: requests.Response, max_bytes: int) -> requests.Response:
    """Read the response body with a counted, chunked read.

    Content-Length is only used as an optional fast reject; the actual
    read counts bytes and aborts immediately once ``max_bytes`` is
    exceeded.  The response is closed on any error path.  On success the
    body is stored on the real ``requests.Response`` (``_content``), so
    ``.text`` / ``.content`` behave exactly like a normal response.
    """
    try:
        content_length = response.headers.get("content-length")
        if content_length is not None and content_length.isdigit():
            if int(content_length) > max_bytes:
                raise ResponseTooLargeError(
                    f"Response body too large (Content-Length {content_length} > {max_bytes} bytes)"
                )
        chunks = []
        total = 0
        for chunk in response.iter_content(DEFAULT_FETCH_READ_CHUNK_BYTES):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLargeError(
                    f"Response body exceeded {max_bytes} bytes"
                )
            chunks.append(chunk)
    except BaseException:
        response.close()
        raise
    response._content = b"".join(chunks)
    response._content_consumed = True
    return response


def _assert_public_peer(sock) -> None:
    """Reject a connection whose actual remote address is not public.

    ``validate_public_url`` checks the addresses a hostname resolves to
    *before* the request.  A DNS rebinding target can resolve public during
    that check and then resolve private for the real connection.  This
    re-applies the same public-address policy to the socket that was
    actually connected, so the gap between validation and connect is
    closed.  Fail closed: if the peer cannot be determined it is rejected.
    """
    if sock is None:
        raise BlockedUrlError("Connection has no peer address to validate")
    try:
        peer = sock.getpeername()
    except OSError as exc:
        raise BlockedUrlError(f"Could not determine connected peer address: {exc}") from exc
    if not peer:
        raise BlockedUrlError("Connection has no peer address")
    host = str(peer[0])
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError as exc:
        raise BlockedUrlError(f"Connected peer is not an IP address: {host!r}") from exc
    if isinstance(address, ipaddress.IPv4Address):
        public = _ipv4_is_public(address)
    else:
        public = _ipv6_is_public(address)
    if not public:
        raise BlockedUrlError(f"Connected to a non-public address: {host!r}")


class _PublicPeerHTTPConnection(urllib3.connection.HTTPConnection):
    """HTTP connection that validates the connected peer address."""

    def connect(self) -> None:
        super().connect()
        _assert_public_peer(self.sock)


class _PublicPeerHTTPSConnection(urllib3.connection.HTTPSConnection):
    """HTTPS connection that validates the connected peer address."""

    def connect(self) -> None:
        super().connect()
        _assert_public_peer(self.sock)


class _PublicPeerHTTPConnectionPool(urllib3.connectionpool.HTTPConnectionPool):
    ConnectionCls = _PublicPeerHTTPConnection


class _PublicPeerHTTPSConnectionPool(urllib3.connectionpool.HTTPSConnectionPool):
    ConnectionCls = _PublicPeerHTTPSConnection


class _PublicAddressPoolManager(urllib3.PoolManager):
    """Pool manager that builds peer-validating connections for every hop."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pool_classes_by_scheme = {
            "http": _PublicPeerHTTPConnectionPool,
            "https": _PublicPeerHTTPSConnectionPool,
        }


class _PublicAddressAdapter(HTTPAdapter):
    """requests adapter whose connections validate the connected peer."""

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        self._pool_connections = connections
        self._pool_maxsize = maxsize
        self._pool_block = block
        self.poolmanager = _PublicAddressPoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )


def _build_public_session() -> requests.Session:
    """Session for a validated public-internet fetch.

    Environment proxies are deliberately ignored: a proxy is an unvalidated
    hop whose own resolution is outside this boundary, and the peer check
    must see the target's address, not the proxy's.  Each new connection
    (initial target and every redirect hop) validates its connected peer.
    """
    session = requests.Session()
    session.trust_env = False
    adapter = _PublicAddressAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def safe_get(
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout=None,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_bytes: int,
) -> requests.Response:
    """GET ``url`` following redirects, validating every target.

    The initial URL and each redirect Location are validated with
    :func:`validate_public_url` before that hop is requested, so a
    redirect cannot escape the public-target policy.  In addition the
    address actually connected to on every hop is re-validated after the
    connection is established (see :func:`_assert_public_peer`), closing
    the DNS-rebinding gap between validation and connect.  Timeout and
    requests exception semantics are unchanged; ``requests.Timeout`` and
    ``requests.RequestException`` propagate exactly like a plain
    ``requests.get`` call.

    The response body is read with a counted, chunked read bounded by
    ``max_bytes`` (see :func:`_read_bounded`); an oversized body raises
    :class:`ResponseTooLargeError`.  Redirect responses are closed
    without reading their bodies.
    """
    session = _build_public_session()
    try:
        current = url
        for _ in range(max_redirects + 1):
            validate_public_url(current)
            response = session.get(
                current,
                params=params,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    try:
                        return _read_bounded(response, max_bytes)
                    except BaseException:
                        response.close()
                        raise
                response.close()
                current = urljoin(current, location)
                continue
            return _read_bounded(response, max_bytes)
    finally:
        session.close()
    raise requests.TooManyRedirects(f"Exceeded {max_redirects} redirects for {url}")
