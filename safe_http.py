"""Central server-side fetch boundary with public-internet target validation.

Every FXRoute-internal HTTP(S) fetch of a URL that is influenced by API
input goes through :func:`safe_get`.  Targets must use only http:// or
https://, must carry no userinfo, and must resolve to publicly routable
addresses.  Every redirect hop is re-validated with the same rules before
it is followed, so a redirect cannot bypass the initial check.

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

DEFAULT_MAX_REDIRECTS = 5

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


def safe_get(
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout=None,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> requests.Response:
    """GET ``url`` following redirects, validating every target.

    The initial URL and each redirect Location are validated with
    :func:`validate_public_url` before that hop is requested, so a
    redirect cannot escape the public-target policy.  Timeout and
    requests exception semantics are unchanged; ``requests.Timeout`` and
    ``requests.RequestException`` propagate exactly like a plain
    ``requests.get`` call.
    """
    current = url
    for _ in range(max_redirects + 1):
        validate_public_url(current)
        response = requests.get(
            current,
            params=params,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        if not response.is_redirect:
            return response
        location = response.headers.get("Location")
        if not location:
            return response
        current = urljoin(current, location)
    raise requests.TooManyRedirects(f"Exceeded {max_redirects} redirects for {url}")
