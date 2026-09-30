#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Feed raw PCM from stdin into the running FXRoute audio service."""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.stdin_protocol import CHUNK_BYTES, MAX_HEADER_BYTES, parse_spec, socket_path


class _Interrupted(BaseException):
    def __init__(self, number: int):
        self.number = number


def _interrupt(number, _frame):
    raise _Interrupted(number)


def _send_pcm(peer: socket.socket) -> int:
    accepted = ready = stdin_eof = half_closed = False
    deadline = time.monotonic() + 5
    pending = b""
    replies = b""
    peer.setblocking(False)
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise RuntimeError("FXRoute did not acknowledge the PCM stream")
        poller = select.poll()
        peer_events = select.POLLIN | select.POLLHUP | select.POLLERR
        if pending:
            peer_events |= select.POLLOUT
        poller.register(peer.fileno(), peer_events)
        if ready and not stdin_eof and len(pending) < CHUNK_BYTES:
            poller.register(0, select.POLLIN | select.POLLHUP | select.POLLERR)
        for fd, events in poller.poll(500):
            if fd == peer.fileno():
                if events & (select.POLLIN | select.POLLHUP | select.POLLERR):
                    try:
                        incoming = peer.recv(MAX_HEADER_BYTES)
                    except BlockingIOError:
                        incoming = None
                    if incoming == b"":
                        raise RuntimeError("FXRoute closed the stream without a result")
                    if incoming:
                        replies += incoming
                    while b"\n" in replies:
                        line, replies = replies.split(b"\n", 1)
                        if len(line) + 1 > MAX_HEADER_BYTES:
                            raise RuntimeError("FXRoute response is too long")
                        response = json.loads(line)
                        event = response.get("event") if isinstance(response, dict) else None
                        if event == "error":
                            raise RuntimeError(str(response.get("message") or "PCM stream failed"))
                        if event == "accepted" and not accepted:
                            accepted = True
                            deadline = None
                        elif event == "ready" and accepted and not ready:
                            ready = True
                        elif event == "done" and half_closed:
                            return 0
                        else:
                            raise RuntimeError("Unexpected FXRoute PCM response")
                    if len(replies) >= MAX_HEADER_BYTES:
                        raise RuntimeError("FXRoute response is too long")
                if events & select.POLLOUT and pending:
                    try:
                        pending = pending[peer.send(pending):]
                    except BlockingIOError:
                        pass
            elif fd == 0:
                chunk = os.read(0, CHUNK_BYTES - len(pending))
                if chunk:
                    pending += chunk
                else:
                    stdin_eof = True
        if stdin_eof and not pending and not half_closed:
            peer.shutdown(socket.SHUT_WR)
            half_closed = True
            deadline = time.monotonic() + 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stdin = commands.add_parser("stdin", help="Send interleaved raw PCM from stdin")
    stdin.add_argument("--format", required=True, choices=("s16le", "s24le", "s32le", "f32le"))
    stdin.add_argument("--rate", required=True, type=int)
    stdin.add_argument("--channels", required=True, type=int)
    stdin.add_argument("--left", type=int, help="1-based channel for the left program input")
    stdin.add_argument("--right", type=int, help="1-based channel for the right program input")
    stdin.add_argument("--socket", help="Override the local FXRoute socket path")
    args = parser.parse_args(argv)
    metadata = dict(version=1, format=args.format, rate=args.rate, channels=args.channels)
    for name in ("left", "right"):
        if getattr(args, name) is not None:
            metadata[name] = getattr(args, name)
    try:
        spec = parse_spec(metadata)
        path = socket_path(args.socket)
    except ValueError as exc:
        parser.error(str(exc))
    previous = {number: signal.signal(number, _interrupt) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(5)
            peer.connect(str(path))
            peer.sendall((json.dumps(spec.header(), separators=(",", ":")) + "\n").encode())
            return _send_pcm(peer)
    except _Interrupted as exc:
        return 128 + exc.number
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"fxroute stdin: {exc}", file=sys.stderr)
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
