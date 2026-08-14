#!/usr/bin/env python3
"""DNS-rebinding hardening tests for the safe_http fetch boundary.

``validate_public_url`` checks the addresses a hostname resolves to before the
request; the connection is then validated again on the address actually
connected.  These tests cover the connected-peer policy and prove that a
public-then-private rebinding is rejected even though the pre-connect DNS
check saw a public address.  No real internet or LAN targets are contacted;
the one end-to-end case connects only to a loopback listener started by the
test itself.
"""

import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import safe_http


def fake_dns_public(host, port=None, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port or 0))]


class _FakePeerSock:
    def __init__(self, peer):
        self._peer = peer

    def getpeername(self):
        return self._peer


def _connected_to(peer):
    """Replace the real connect with a fake already-connected socket."""

    def _connect(self):
        self.sock = _FakePeerSock(peer)

    return _connect


class _FakeRedirectResponse:
    def __init__(self, location):
        self.is_redirect = True
        self.headers = {"Location": location}
        self.closed = False

    def close(self):
        self.closed = True


class PublicPeerAssertionTests(unittest.TestCase):
    def test_private_ipv4_peers_rejected(self):
        for host in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1",
                     "172.16.0.1", "100.64.0.1", "0.0.0.0", "224.0.0.1"):
            with self.subTest(host=host):
                with self.assertRaises(safe_http.BlockedUrlError):
                    safe_http._assert_public_peer(_FakePeerSock((host, 80)))

    def test_private_and_special_ipv6_peers_rejected(self):
        for host in ("::1", "fe80::1", "fc00::1", "fd00::1", "ff02::1", "::"):
            with self.subTest(host=host):
                with self.assertRaises(safe_http.BlockedUrlError):
                    safe_http._assert_public_peer(_FakePeerSock((host, 80, 0, 0)))

    def test_embedded_private_ipv4_peers_rejected(self):
        for host in ("::ffff:127.0.0.1", "::ffff:10.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::"):
            with self.subTest(host=host):
                with self.assertRaises(safe_http.BlockedUrlError):
                    safe_http._assert_public_peer(_FakePeerSock((host, 80, 0, 0)))

    def test_public_peers_accepted(self):
        safe_http._assert_public_peer(_FakePeerSock(("8.8.8.8", 443)))
        safe_http._assert_public_peer(
            _FakePeerSock(("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0))
        )

    def test_undeterminable_peer_fails_closed(self):
        with self.assertRaises(safe_http.BlockedUrlError):
            safe_http._assert_public_peer(None)

        class _Raises:
            def getpeername(self):
                raise OSError("no peer")

        with self.assertRaises(safe_http.BlockedUrlError):
            safe_http._assert_public_peer(_Raises())

        with self.assertRaises(safe_http.BlockedUrlError):
            safe_http._assert_public_peer(_FakePeerSock(()))

        with self.assertRaises(safe_http.BlockedUrlError):
            safe_http._assert_public_peer(_FakePeerSock(("not-an-ip", 80)))


class ConnectionValidationTests(unittest.TestCase):
    def test_http_connection_rejects_private_peer(self):
        conn = safe_http._PublicPeerHTTPConnection("public.example", 80)
        with patch.object(urllib3.connection.HTTPConnection, "connect",
                          _connected_to(("10.0.0.5", 80))):
            with self.assertRaises(safe_http.BlockedUrlError):
                conn.connect()

    def test_http_connection_accepts_public_peer(self):
        conn = safe_http._PublicPeerHTTPConnection("public.example", 80)
        with patch.object(urllib3.connection.HTTPConnection, "connect",
                          _connected_to(("8.8.8.8", 80))):
            conn.connect()
        self.assertIsNotNone(conn.sock)

    def test_https_connection_rejects_private_peer(self):
        conn = safe_http._PublicPeerHTTPSConnection("public.example", 443)
        with patch.object(urllib3.connection.HTTPSConnection, "connect",
                          _connected_to(("127.0.0.1", 443))):
            with self.assertRaises(safe_http.BlockedUrlError):
                conn.connect()

    def test_https_connection_accepts_public_peer(self):
        conn = safe_http._PublicPeerHTTPSConnection("public.example", 443)
        with patch.object(urllib3.connection.HTTPSConnection, "connect",
                          _connected_to(("8.8.8.8", 443))):
            conn.connect()
        self.assertIsNotNone(conn.sock)


class SessionWiringTests(unittest.TestCase):
    def test_build_public_session_ignores_proxies_and_mounts_adapter(self):
        session = safe_http._build_public_session()
        try:
            self.assertFalse(session.trust_env)
            self.assertIsInstance(session.get_adapter("http://"), safe_http._PublicAddressAdapter)
            self.assertIsInstance(session.get_adapter("https://"), safe_http._PublicAddressAdapter)
        finally:
            session.close()


class RebindingIntegrationTests(unittest.TestCase):
    def test_public_validation_then_private_connect_is_blocked(self):
        """A rebinding target that resolves public on validation but connects
        private must be rejected by the post-connect peer check."""
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        calls = []

        def rebinding_dns(host, port_, *args, **kwargs):
            calls.append((host, port_))
            if len(calls) == 1:
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port_ or 0))]
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port_))]

        try:
            with patch.object(safe_http.socket, "getaddrinfo", side_effect=rebinding_dns):
                with self.assertRaises(safe_http.BlockedUrlError):
                    safe_http.safe_get(f"http://rebind.example:{port}/x", timeout=3, max_bytes=1024)
        finally:
            listener.close()

        self.assertGreaterEqual(len(calls), 2, "validation and connect both resolved the host")
        self.assertIsNone(calls[0][1], "validation resolves without a port")
        self.assertEqual(calls[1][1], port, "connect resolves with the request port")


class RedirectRebindingTests(unittest.TestCase):
    def test_connection_block_on_redirect_hop_propagates(self):
        """A redirect hop whose connection is rejected by the peer check must
        propagate the block and never read a body."""
        def side_effect(url, **kwargs):
            if url == "https://public.example/a.pls":
                return _FakeRedirectResponse("https://cdn.example/b.pls")
            raise safe_http.BlockedUrlError("Connected to a non-public address: '10.0.0.5'")

        session = MagicMock()
        session.get.side_effect = side_effect
        with patch.object(safe_http.socket, "getaddrinfo", side_effect=fake_dns_public), \
                patch.object(safe_http, "_build_public_session", return_value=session):
            with self.assertRaises(safe_http.BlockedUrlError):
                safe_http.safe_get("https://public.example/a.pls", timeout=5, max_bytes=1024)
        self.assertEqual(session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
