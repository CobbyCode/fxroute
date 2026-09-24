# SPDX-License-Identifier: AGPL-3.0-only
"""Hold a capture source open across a speaker job (input keeper).

A keeper tap keeps the microphone source streaming between way captures so
no suspend/resume cycle can slip the mic timing by an ALSA period mid-run.
It records nothing: a ``pw-record`` stream to the null device holds the
links whose only purpose is keeping the device awake.

This adapter resolves inputs, spawns and links, but never captures, stages
or commits. Failure is loud: an unresolvable input, an unlinkable port or
an unverified link aborts before any sweep is spent.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from contextlib import asynccontextmanager
from typing import Any

from measurement.routing import LINK_SETTLE_SECONDS

logger = logging.getLogger(__name__)

KEEPER_NODE_PREFIX = "fxroute-input-keeper"
KEEPER_PORT_DISCOVERY_TIMEOUT_SECONDS = 4.0
KEEPER_PORT_DISCOVERY_POLL_SECONDS = 0.1
KEEPER_STOP_TIMEOUT_SECONDS = 2.0
KEEPER_KILL_TIMEOUT_SECONDS = 2.0


def reap_keeper_process(process: Any, *, timeout: float = KEEPER_STOP_TIMEOUT_SECONDS) -> None:
    """Wait for a stopped keeper so no defunct child stays in the process table.

    ``terminate()`` only signals: without the wait the keeper remains a zombie
    under the app until the app itself exits. A keeper that ignores SIGTERM is
    killed and reaped as well. Objects without a real child are tolerated.
    """
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return
    try:
        process.kill()
    except Exception:
        logger.warning("Input keeper process could not be killed", exc_info=True)
    try:
        process.wait(timeout=KEEPER_KILL_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("Input keeper process was not reaped after SIGKILL", exc_info=True)


def _forget_keeper_process(store: Any, owner_id: str, process: Any) -> None:
    """Unregister a reaped keeper so no dead Popen reference stays behind.

    The keeper owns its own registry key, so nothing else would ever remove
    it. Stores without the seam (test doubles) are tolerated.
    """
    forget = getattr(store, "_forget_job_process", None)
    if not callable(forget):
        return
    try:
        forget(owner_id, process)
    except Exception:
        logger.warning("Input keeper process registry cleanup failed", exc_info=True)


def _keeper_node_name(owner: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", str(owner or "").strip()).strip("-")
    return f"{KEEPER_NODE_PREFIX}-{slug or 'job'}"[:64]


def _mic_channel_index(value: object, channel_count: int) -> int:
    try:
        index = int(str(value or "").strip() or 1) - 1
    except (TypeError, ValueError):
        index = 0
    return max(0, min(index, max(0, int(channel_count or 1) - 1)))


class InputKeeper:
    """Own one keeper stream; ``active`` mirrors the spawned process."""

    def __init__(self, *, node_name: str, process: Any, release: Any = None):
        self.node_name = node_name
        self._process = process
        self._release = release
        self.active = True

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        try:
            if self._process.poll() is None:
                self._process.terminate()
        except Exception:
            pass
        # Reap unconditionally: a keeper that exited on its own is still a
        # defunct child until someone waits for it.
        reap_keeper_process(self._process)
        # Then drop it from the process registry: a reaped keeper must not
        # remain a dead Popen reference.
        if callable(self._release):
            self._release()


@asynccontextmanager
async def input_keeper_scope(
    store: Any,
    *,
    input_id: str,
    mic_input_channel: str | int | None = "1",
    owner: str,
):
    """Hold the selected mic source open; release (and unlink) on exit.

    Resolves the input through the store, spawns a discard tap, links the
    mic port, verifies the link and settles. Every failure raises before
    yielding; exit always stops the process and best-effort unlinks.
    """
    if not isinstance(input_id, str) or not input_id.strip():
        raise ValueError("Input keeper requires a capture input id")
    inputs = store._measurement_inputs_with_sample_rate(
        await asyncio.to_thread(store._cached_capture_inputs)
    )
    selected = store._resolve_capture_input(inputs, input_id=input_id)
    source_node = str(selected.get("node_name") or "").strip()
    if not source_node or source_node.endswith(".monitor"):
        raise ValueError("Input keeper requires a real microphone source")
    channels = selected.get("channels") or 0
    mic_index = _mic_channel_index(mic_input_channel, channels)
    rate = selected.get("measurement_sample_rate") or selected.get("sample_rate") or 48000
    try:
        rate = int(rate)
    except (TypeError, ValueError):
        rate = 48000
    node_name = _keeper_node_name(owner)
    routing = store._routing
    mic_ports = routing._list_source_output_ports(source_node)
    mic_port = routing._pick_port(
        mic_ports, routing._port_suffixes_for_channel_index(mic_index)
        + [":capture_MONO", ":output_MONO"])
    if not mic_port:
        raise ValueError("Input keeper found no microphone port to hold open")
    command = [
        "pw-record", "-P", "node.autoconnect=false", "-P", f"node.name={node_name}",
        "--target", "0", "--rate", str(rate), "--channels", "2",
        "--format", "s16", "/dev/null",
    ]
    owner_id = f"keeper:{node_name}"
    process = store._start_job_process(owner_id, command)
    keeper = InputKeeper(
        node_name=node_name, process=process,
        release=lambda: _forget_keeper_process(store, owner_id, process))
    try:
        deadline = time.monotonic() + KEEPER_PORT_DISCOVERY_TIMEOUT_SECONDS
        keeper_inputs: list[str] = []
        while time.monotonic() < deadline:
            keeper_inputs = [port for port in store._list_pw_ports(node_name) if ":input_" in port]
            if keeper_inputs:
                break
            await asyncio.sleep(KEEPER_PORT_DISCOVERY_POLL_SECONDS)
        if not keeper_inputs:
            raise RuntimeError("Input keeper record inputs never appeared")
        keeper_input = routing._pick_port(
            keeper_inputs, [":input_FL", ":input_MONO", ":input_AUX0", ":input_FR"])
        if not keeper_input:
            raise RuntimeError("Input keeper found no record input to link")
        store._create_pipewire_link(mic_port, keeper_input)
        verified = routing._verify_record_links([(mic_port, keeper_input, "keeper-mic-to-record")])
        if verified.get("keeper-mic-to-record") is None:
            raise RuntimeError("Input keeper link never appeared in listing")
        time.sleep(LINK_SETTLE_SECONDS)
        logger.info("Input keeper holding %s open for %s", mic_port, owner_id)
        yield keeper
    finally:
        keeper.stop()
        try:
            routing._cleanup_fxroute_links(source_node_name=source_node, record_node_name=node_name)
        except Exception:
            logger.warning("Input keeper link cleanup failed", exc_info=True)
