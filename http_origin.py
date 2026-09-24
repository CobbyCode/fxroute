# SPDX-License-Identifier: AGPL-3.0-only

"""Request-trust classification for the ASGI edge.

Owns FXRoute's proxy-aware same-origin defence: effective scheme/host/port
resolution (a forwarded ``X-Forwarded-*`` hop is honored only from trusted
proxy peers), the cross-site verdict for ``Origin``/``Referer`` headers,
and the Host allowlist that stops DNS-rebinding -- a rebound name makes the
browser's ``Origin`` and ``Host`` agree, so same-origin comparison alone
cannot catch it.

The classification is pure request handling: no application state, locks,
or I/O. Malformed ports fail closed (untrusted) instead of raising, and
headerless CLI/systemd callers stay allowed. ``TrustedOriginMiddleware``
applies it centrally: the Host allowlist on every HTTP request, the
same-origin verdict on unsafe methods, and both checks on WebSocket
handshakes so ``/ws`` never exposes state to a foreign page. Privileged
routes may retain their local checks as defense in depth.
"""

from __future__ import annotations

import ipaddress
import json
import os
from typing import Optional
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket


UNSAFE_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Default Host-allowlist suffixes.  Literal IPs and single-label names are
# always allowed (see ``is_host_allowed``); these extra suffixes cover the
# LAN naming conventions FXRoute installs actually use.  None of them are
# delegatable in public DNS, so a remote page cannot claim a name here for
# a DNS-rebinding attack.  ``FXROUTE_ALLOWED_HOSTS`` extends the list for
# reverse-proxy hostnames: comma-separated exact names or ``.suffix`` rules.
_BUILTIN_ALLOWED_HOST_SUFFIXES = (".local", ".lan", ".home.arpa")

# Direct TCP peers whose ``X-Forwarded-*`` headers are honored.  Loopback
# covers the documented Caddy-on-the-same-box setup (matching uvicorn's
# default ``forwarded_allow_ips``); ``FXROUTE_TRUSTED_PROXIES`` adds peers
# (comma-separated) for a proxy on another host.
_TRUSTED_PROXY_PEERS = frozenset({"127.0.0.1", "::1", "localhost"})


def _env_list(name: str) -> list[str]:
    return [
        entry.strip().lower()
        for entry in os.environ.get(name, "").split(",")
        if entry.strip()
    ]


def _peer_is_trusted_proxy(request: HTTPConnection) -> bool:
    """True when the direct TCP peer may supply ``X-Forwarded-*`` headers.

    Blind trust would let any raw caller forge the effective host and walk
    past both the same-origin verdict and the Host allowlist.  Untrusted or
    missing peer information fails closed (headers ignored).
    """

    scope = getattr(request, "scope", None)
    client = scope.get("client") if scope else None
    peer = str(client[0]).strip().lower() if client else ""
    if not peer:
        return False
    if peer in _TRUSTED_PROXY_PEERS:
        return True
    return peer in _env_list("FXROUTE_TRUSTED_PROXIES")


def _forwarded_header(request: HTTPConnection, name: str) -> str:
    """First value of a forwarded header; empty string for untrusted peers."""

    if not _peer_is_trusted_proxy(request):
        return ""
    return (request.headers.get(name) or "").split(",", 1)[0].strip().lower()


def effective_request_scheme(request: HTTPConnection) -> str:
    forwarded_proto = _forwarded_header(request, "x-forwarded-proto")
    scheme = forwarded_proto or (request.url.scheme or "http").lower()
    scope = getattr(request, "scope", None)
    if scope is not None and scope.get("type") == "websocket":
        # Browsers send Origin: http(s):// on WebSocket handshakes while
        # the ASGI scope speaks ws(s); compare both on HTTP terms.
        scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    return scheme


def effective_request_host(request: HTTPConnection) -> str:
    """Resolve the host the client *thinks* it is talking to.

    Honors ``X-Forwarded-Host`` when the install is behind a reverse proxy
    (a single hop from a trusted proxy peer only, matching the project's
    trust assumptions for the optional Caddy reverse proxy on the LAN).
    The value is lower-cased and stripped of optional ``:port`` so it can
    be compared against ``Origin`` / ``Referer`` headers verbatim.
    """

    forwarded_host = _forwarded_header(request, "x-forwarded-host")
    if forwarded_host:
        # Hostname only -- the matching port lives in X-Forwarded-Port.
        return forwarded_host.split(":", 1)[0]
    return (request.url.hostname or "").lower()


def effective_request_port(request: HTTPConnection) -> Optional[int]:
    """Resolve the frontend port the client believes it is talking to.

    Honors ``X-Forwarded-Port`` from trusted proxy peers so the optional
    Caddy reverse proxy on the LAN is supported without losing the
    same-origin defence.  When no forward headers apply, the request's
    actual URL port is used.
    """

    raw = _forwarded_header(request, "x-forwarded-port")
    if raw.isdigit():
        return int(raw)
    try:
        return request.url.port
    except ValueError:
        # Malformed Host port: return a sentinel that can never equal a
        # parsed header port (those are 0-65535 or None) and never passes
        # the default-port rules, so the origin comparison fails closed
        # instead of raising.
        return -1


class TrustedOriginMiddleware:
    """Edge gate: Host allowlist, same-origin verdict, WebSocket handshakes.

    Every HTTP request must name an allowed Host; unsafe methods also need
    a trusted ``Origin``/``Referer``.  WebSocket handshakes get both checks,
    so a foreign page can neither read ``/ws`` state nor ride a rebinding
    name past the same-origin comparison.  Rejections are plain 403s sent
    before route dispatch.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if scope_type == "websocket":
            detail = websocket_handshake_rejection(WebSocket(scope, receive, send))
            if detail is not None:
                await send_websocket_denial(scope, send, detail)
                return
        elif scope_type == "http":
            request = Request(scope, receive=receive)
            detail = http_request_rejection_detail(
                request, str(scope.get("method") or "")
            )
            if detail is not None:
                response = JSONResponse(
                    status_code=403,
                    content={"detail": detail},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def is_request_origin_trusted(request: HTTPConnection) -> bool:
    """Cross-site defence for state-changing endpoints.

    FXRoute's LAN security baseline is "trusted LAN, no auth, no cookies"
    (see ``AGENTS.md``).  Within that baseline, requests originating from
    a foreign web page in the same browser are *not* a trusted caller, so
    we cannot rely solely on the LAN assumption.

    The routine therefore refuses an unsafe request whose ``Origin`` or
    ``Referer`` header points to a different scheme + host + port than the one
    the request is actually reaching -- the cheap, no-cookie CSRF defence
    recommended when an application cannot introduce a new auth surface.
    Requests without either header (the legitimate CLI / curl / systemd
    path) are allowed so existing LAN operators do not lose their
    workflows.

    Returns ``True`` when the call is allowed, ``False`` when it must be
    rejected as a cross-site POST.
    """

    trusted_host = effective_request_host(request)
    if not trusted_host:
        # No host to compare against: the request is malformed enough that
        # we refuse to make a decision and let the caller choose.
        return False

    trusted_scheme = effective_request_scheme(request)
    # If the request did not run on a known port (httpx test client with
    # weird hosts) the comparison falls back to comparing host only.
    trusted_port = effective_request_port(request)

    def _netloc_matches(parsed) -> bool:
        if not parsed.hostname:
            return False
        if parsed.hostname.lower() != trusted_host:
            return False
        try:
            header_port = parsed.port
        except ValueError:
            # Malformed header port (out of range / non-numeric): refuse the
            # request instead of letting the comparison raise a 500.
            return False
        if header_port is None and trusted_port in (None, 80, 443):
            return True
        if header_port is None:
            # Header did not include a port; fall back to comparing
            # against the request's effective default.
            if (trusted_scheme == "https" and trusted_port == 443) or (
                trusted_scheme == "http" and trusted_port in (None, 80)
            ):
                return True
            return False
        return header_port == trusted_port

    origin = (request.headers.get("origin") or "").strip().lower()
    if origin:
        if origin == "null":
            # Browsers emit Origin: null for sandboxed documents and
            # cross-origin redirects under specific referrer policies.  We
            # cannot confirm the caller's site, so refuse.
            return False
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        if parsed.scheme and parsed.scheme.lower() != trusted_scheme:
            return False
        return _netloc_matches(parsed)

    referer = (
        request.headers.get("referer")
        or request.headers.get("referrer")
        or ""
    ).strip()
    if referer:
        try:
            parsed = urlparse(referer)
        except ValueError:
            return False
        if parsed.scheme and parsed.scheme.lower() != trusted_scheme:
            return False
        return _netloc_matches(parsed)

    # No Origin AND no Referer: a CLI / systemd caller.  Allowed because
    # the LAN security baseline treats direct callers as trusted.
    return True


def is_host_allowed(host: Optional[str]) -> bool:
    """Host-allowlist verdict: may this name identify the install?

    DNS rebinding makes a foreign page's ``Origin`` and the request's
    ``Host`` agree on the attacker's name, which satisfies the same-origin
    check -- so the host itself must be verified separately:

    * literal IPs are always allowed (an address cannot be rebound),
    * single-label names are always allowed (``localhost``, short machine
      names; not claimable in public DNS),
    * the built-in LAN suffixes ``.local`` / ``.lan`` / ``.home.arpa``
      are allowed,
    * every other dotted name must appear in ``FXROUTE_ALLOWED_HOSTS``
      (comma-separated exact names or ``.suffix`` rules).

    Returns ``True`` when the host may be used, ``False`` otherwise.
    """

    if not host:
        return False
    name = host.strip().lower().rstrip(".")
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if "." not in name:
        return True
    rules = [*_env_list("FXROUTE_ALLOWED_HOSTS"), *_BUILTIN_ALLOWED_HOST_SUFFIXES]
    for rule in rules:
        if rule.startswith("."):
            if name.endswith(rule):
                return True
        elif name == rule:
            return True
    return False


def is_request_host_allowed(request: HTTPConnection) -> bool:
    """Allowlist check for the effective Host of ``request``."""

    return is_host_allowed(effective_request_host(request))


def http_request_rejection_detail(request: HTTPConnection, method: str) -> Optional[str]:
    """Central HTTP verdict for the middleware; ``None`` lets the call run.

    The Host allowlist applies to every method (it is the DNS-rebinding
    gate); the same-origin verdict still covers unsafe methods only.
    """

    if not is_request_host_allowed(request):
        return "Host not allowed"
    if method.upper() in UNSAFE_HTTP_METHODS and not is_request_origin_trusted(request):
        return "Cross-site request rejected"
    return None


def websocket_handshake_rejection(request: HTTPConnection) -> Optional[str]:
    """WebSocket-handshake verdict; ``None`` lets the upgrade proceed.

    Browsers always send ``Origin`` on a handshake, so the same rules as
    unsafe HTTP methods apply (headerless non-browser clients stay
    allowed, matching the CLI baseline).  The Host allowlist also covers
    the handshake: a rebinding page's ``ws://attacker.name`` upgrade would
    otherwise agree with itself and pass the same-origin check.
    """

    if not is_request_host_allowed(request):
        return "Host not allowed"
    if not is_request_origin_trusted(request):
        return "Cross-site request rejected"
    return None


async def send_websocket_denial(scope: Scope, send: Send, detail: str) -> None:
    """Deny a WebSocket handshake before the endpoint sees any request.

    Prefers the ASGI ``websocket.http.response`` extension (advertised by
    uvicorn and the Starlette test client) so the caller gets a real 403
    with a JSON body; falls back to a policy-violation close, which
    uvicorn answers with an empty 403 handshake denial.
    """

    if "websocket.http.response" in scope.get("extensions", {}):
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send(
            {
                "type": "websocket.http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "websocket.http.response.body", "body": body})
    else:
        await send(
            {
                "type": "websocket.close",
                "code": 1008,
                "reason": "policy violation",
            }
        )
