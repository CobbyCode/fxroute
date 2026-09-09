# SPDX-License-Identifier: AGPL-3.0-only

"""Trusted-origin determination for state-changing endpoints.

Extracted verbatim from main.py (REFACTOR-015). Behavior is identical to
the previous inline implementation. This module owns the proxy-aware
same-origin defence: effective scheme/host/port resolution (honoring the
single trusted ``X-Forwarded-*`` hop of the optional LAN reverse proxy)
and the cross-site verdict for ``Origin``/``Referer`` headers.

It is pure request classification: no application state, no locks, no I/O.
Malformed ports fail closed (untrusted) instead of raising, and headerless
CLI/systemd callers stay allowed. All consumers (update/restore routes,
device-name route, streaming provider admin, power API) share these
predicates instead of reimplementing the comparison.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from fastapi import Request


def effective_request_scheme(request: Request) -> str:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if forwarded_proto:
        return forwarded_proto
    return (request.url.scheme or "http").lower()


def effective_request_host(request: Request) -> str:
    """Resolve the host the client *thinks* it is talking to.

    Honors ``X-Forwarded-Host`` when the install is behind a reverse proxy
    (FXRoute only ever trusts a single hop here, matching the project's
    trust assumptions for the optional Caddy reverse proxy on the LAN).
    The value is lower-cased and stripped of optional ``:port`` so it can
    be compared against ``Origin`` / ``Referer`` headers verbatim.
    """

    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip().lower()
    if forwarded_host:
        # Hostname only -- the matching port lives in X-Forwarded-Port.
        return forwarded_host.split(":", 1)[0]
    return (request.url.hostname or "").lower()


def effective_request_port(request: Request) -> Optional[int]:
    """Resolve the frontend port the client believes it is talking to.

    Honors ``X-Forwarded-Port`` so the optional Caddy reverse proxy on
    the LAN is supported without losing the same-origin defence.  When no
    forward headers are set, the request's actual URL port is used.
    """

    raw = (request.headers.get("x-forwarded-port") or "").split(",", 1)[0].strip()
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


def is_request_origin_trusted(request: Request) -> bool:
    """Cross-site defence for state-changing endpoints.

    FXRoute's LAN security baseline is "trusted LAN, no auth, no cookies"
    (see ``AGENTS.md``).  Within that baseline, requests originating from
    a foreign web page in the same browser are *not* a trusted caller, so
    we cannot rely solely on the LAN assumption.

    The routine therefore refuses a POST whose ``Origin`` or ``Referer``
    header points to a different scheme + host + port than the one the
    request is actually reaching -- the cheap, no-cookie CSRF defence
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
