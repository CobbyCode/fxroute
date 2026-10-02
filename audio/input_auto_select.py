# SPDX-License-Identifier: AGPL-3.0-only
"""Input auto-select: switch to an armed input on signal, return when it ends.

Each armed external input gets a capture probe (pw-record linked to its
stereo pair) that feeds an ActivityDetector; STDIN activity is a connected
PCM writer. A rising activity edge on an armed input switches to it and
remembers the selection it replaced (the engagement). Once the engaged
input released (no signal for the release hold), another armed input that
is still active takes over, otherwise the remembered selection is
restored. Any other selection change ends the engagement, so a manual
selection is what the next automatic switch returns to.

No imports from ``main``: the composition root injects the services.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy

from audio import pw_link
from audio.alsa_ctl import read_digital_lock
from audio.input_activity import (
    BLOCK_SECONDS,
    KIND_ANALOG,
    KIND_DIGITAL,
    KIND_STDIN,
    RELEASE_SECONDS,
    ActivityDetector,
    analyze_block,
)
from audio.samplerate import (
    SOURCE_MODE_APP_PLAYBACK,
    SOURCE_MODE_BLUETOOTH_INPUT,
    SOURCE_MODE_EXTERNAL_INPUT,
    SOURCE_MODE_STDIN_INPUT,
)
from common import process_stop
from common.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

STDIN_KEY = SOURCE_MODE_STDIN_INPUT
TICK_SECONDS = 0.5
# Idle inputs rearm after a short absence; only the engagement needs a long hold.
REARM_SECONDS = 1.0
OVERVIEW_MAX_AGE_SECONDS = 15.0
CAPTURE_RATE_REFRESH_SECONDS = 15.0
PROBE_LINK_TIMEOUT_SECONDS = 3.0
PROBE_LINK_RETRY_SECONDS = 0.15
PROBE_NO_DATA_TIMEOUT_SECONDS = 3.0
PROBE_RESTART_MAX_SECONDS = 10.0
# A capture that ran this long counts as healthy: the restart delay starts over.
PROBE_HEALTHY_SECONDS = 30.0
PROBE_STOP_GRACE_SECONDS = 1.0
LOCK_POLL_SECONDS = 1.0
PROBE_NODE_PREFIX = "fxroute_input_probe"
_PROBE_SEQUENCE = count(1)

_SOURCE_MODES = {
    SOURCE_MODE_APP_PLAYBACK,
    SOURCE_MODE_EXTERNAL_INPUT,
    SOURCE_MODE_BLUETOOTH_INPUT,
    SOURCE_MODE_STDIN_INPUT,
}
_DIGITAL_HINTS = ("iec958", "spdif", "s/pdif", "optical", "coax", "toslink")
_STDIN_PRESENT_STATES = {"connected", "ready", "streaming", "draining"}

Target = tuple[str, "str | None"]


def default_state_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_root / "fxroute" / "input-auto-select.json"


def selection_key(selection: dict[str, Any]) -> str:
    """Key of a persisted selection: the input key or the mode name."""
    mode = selection.get("mode") or SOURCE_MODE_APP_PLAYBACK
    if mode == SOURCE_MODE_EXTERNAL_INPUT:
        return str(selection.get("selected_input_key") or "")
    return str(mode)


def _target_for_key(key: str) -> Target:
    if key == STDIN_KEY:
        return (SOURCE_MODE_STDIN_INPUT, None)
    return (SOURCE_MODE_EXTERNAL_INPUT, key)


def _is_digital_entry(entry: dict[str, Any]) -> bool:
    text = " ".join(
        str(entry.get(name) or "")
        for name in ("source_key", "name", "port_key", "port_label", "device_label", "pair_label")
    ).lower()
    return any(hint in text for hint in _DIGITAL_HINTS)


@dataclass(frozen=True)
class ProbeSpec:
    source_name: str
    left_channel: str
    right_channel: str
    rate: int
    card: int | None
    digital: bool


class InputProbe:
    """One pw-record capture of an input pair feeding its detector.

    The stream is unconnected by default and linked to exactly the two
    source channels, so no remix touches the samples and IEC 61937 words
    stay bit-exact. Restarts with a growing delay on any failure; while
    down, the detector observes the input as missing.
    """

    def __init__(self, spec: ProbeSpec, detector: ActivityDetector, *,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.spec = spec
        self._detector = detector
        self._monotonic = monotonic
        self._task: asyncio.Task | None = None
        self._lock_state: bool | None = None
        self._lock_read_at = -math.inf

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"input-probe:{self.spec.source_name}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        delay = 1.0
        while True:
            started = self._monotonic()
            try:
                await self._capture()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Input probe on %s stopped: %s", self.spec.source_name, exc)
            self._detector.observe_missing(self._monotonic())
            if self._monotonic() - started >= PROBE_HEALTHY_SECONDS:
                delay = 1.0
            await asyncio.sleep(delay)
            delay = min(delay * 2, PROBE_RESTART_MAX_SECONDS)

    async def _capture(self) -> None:
        spec = self.spec
        node_name = f"{PROBE_NODE_PREFIX}_{next(_PROBE_SEQUENCE)}"
        proc = await asyncio.create_subprocess_exec(
            "pw-record",
            "-P", "node.autoconnect=false",
            "-P", f"node.name={node_name}",
            "-P", "node.description=FXRoute input auto-select",
            "--format", "s32",
            "--channels", "2",
            "--rate", str(spec.rate),
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await self._link(proc, node_name)
            block_bytes = max(1, int(spec.rate * BLOCK_SECONDS)) * 8
            pending = bytearray()
            while True:
                chunk = await asyncio.wait_for(proc.stdout.read(block_bytes), PROBE_NO_DATA_TIMEOUT_SECONDS)
                if not chunk:
                    raise RuntimeError(f"pw-record exited with {proc.returncode}")
                pending += chunk
                while len(pending) >= block_bytes:
                    frames = numpy.frombuffer(bytes(pending[:block_bytes]), dtype="<i4").reshape(-1, 2)
                    del pending[:block_bytes]
                    now = self._monotonic()
                    self._detector.observe_block(now, analyze_block(frames), self._read_lock(now))
        finally:
            if await process_stop.stop_command_child_cancellation_safe(
                proc, PROBE_STOP_GRACE_SECONDS, cleanup_log="Input probe pw-record stop failed",
            ):
                raise asyncio.CancelledError

    async def _link(self, proc: asyncio.subprocess.Process, node_name: str) -> None:
        deadline = self._monotonic() + PROBE_LINK_TIMEOUT_SECONDS
        source = self.spec.source_name
        for channel, side in ((self.spec.left_channel, "FL"), (self.spec.right_channel, "FR")):
            source_ports = (f"{source}:capture_{channel}", f"{source}:output_{channel}")
            while True:
                if proc.returncode is not None:
                    raise RuntimeError(f"pw-record exited with {proc.returncode} before linking")
                try:
                    await pw_link.connect_ports(source_ports, f"{node_name}:input_{side}")
                    break
                except (RuntimeError, OSError):
                    # The probe's ports appear shortly after its start.
                    if self._monotonic() >= deadline:
                        raise
                    await asyncio.sleep(PROBE_LINK_RETRY_SECONDS)

    def _read_lock(self, now: float) -> bool | None:
        if not self.spec.digital or self.spec.card is None:
            return None
        if now - self._lock_read_at >= LOCK_POLL_SECONDS:
            self._lock_read_at = now
            self._lock_state = read_digital_lock(self.spec.card)
        return self._lock_state


@dataclass
class InputAutoSelectDependencies:
    """Live services input auto-select needs (bound by main)."""

    # Newest client overview if its full build is at most this old.
    get_fresh_overview: Callable[[float], dict | None]
    # Validated client read; records the overview it builds.
    read_overview: Callable[[], Awaitable[dict]]
    # Re-push the newest overview (with the auto-select snapshot) to clients.
    publish_state: Callable[[], Awaitable[dict | None]]
    load_selection: Callable[[], dict]
    # Decide under the source-transition lock, then commit like a client
    # switch; returns None when the decision declined.
    switch_if: Callable[[Callable[[dict], "Target | None"]], Awaitable[dict | None]]
    get_stdin_status: Callable[[], dict | None]
    measurement_owns_graph: Callable[[], bool]
    # Blocking; always run in a worker thread.
    resolve_capture_rate: Callable[[], int]
    probe_factory: Callable[[ProbeSpec, ActivityDetector], Any] = InputProbe
    state_path: Callable[[], Path] = default_state_path
    monotonic: Callable[[], float] = time.monotonic


class InputAutoSelect:
    """Single owner of the armed inputs, their probes and the engagement."""

    def __init__(self, deps: InputAutoSelectDependencies) -> None:
        self._deps = deps
        self._armed: list[str] = []
        self._engaged: dict[str, Any] | None = None
        self._detectors: dict[str, ActivityDetector] = {}
        self._probes: dict[str, Any] = {}
        self._was_active: dict[str, bool] = {}
        self._paused = False
        self._published: dict | None = None
        self._capture_rate: int | None = None
        self._capture_rate_at = -math.inf
        self._last_error: str | None = None
        self.task: asyncio.Task | None = None
        self._load()

    # Persistence -----------------------------------------------------------

    def _load(self) -> None:
        try:
            payload = json.loads(self._deps.state_path().read_text())
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        armed = payload.get("armed")
        if isinstance(armed, list):
            self._armed = list(dict.fromkeys(key for key in armed if isinstance(key, str) and key))
        engaged = payload.get("engaged")
        if (isinstance(engaged, dict) and engaged.get("key") in self._armed
                and engaged.get("return_mode") in _SOURCE_MODES):
            return_key = engaged.get("return_input_key")
            self._engaged = {
                "key": engaged["key"],
                "return_mode": engaged["return_mode"],
                "return_input_key": return_key if isinstance(return_key, str) and return_key else None,
            }

    def _save(self) -> None:
        try:
            atomic_write_text(self._deps.state_path(), json.dumps(
                {"armed": self._armed, "engaged": self._engaged}, indent=2) + "\n")
        except OSError as exc:
            logger.warning("Failed to save input auto-select state: %s", exc)

    def _set_engaged(self, engaged: dict[str, Any] | None) -> None:
        if engaged != self._engaged:
            self._engaged = dict(engaged) if engaged else None
            for key, detector in self._detectors.items():
                detector.release_seconds = RELEASE_SECONDS if engaged and engaged["key"] == key else REARM_SECONDS
            self._save()

    # Client state ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "armed": list(self._armed),
            "engaged": dict(self._engaged) if self._engaged else None,
            "paused": self._paused,
            "inputs": {
                key: {
                    "active": detector.active,
                    "format": detector.format,
                    "lock": detector.lock,
                }
                for key in self._armed
                if (detector := self._detectors.get(key)) is not None
            },
        }

    def decorate_overview(self, overview: dict) -> dict:
        return {**overview, "auto_select": self.snapshot()}

    async def set_armed(self, key: str, enabled: bool) -> dict | None:
        """Arm or disarm one input; returns the republished overview.

        Disarming the engaged input ends the engagement and stays on it.
        """
        key = (key or "").strip()
        if not key:
            raise ValueError("Input key is required")
        if enabled and key not in self._armed:
            if key != STDIN_KEY:
                overview = self._deps.get_fresh_overview(OVERVIEW_MAX_AGE_SECONDS) or await self._deps.read_overview()
                if not any(entry.get("key") == key for entry in overview.get("inputs") or []
                           if isinstance(entry, dict)):
                    raise ValueError(f"Unknown input: {key}")
            self._armed.append(key)
            self._save()
        elif not enabled and key in self._armed:
            self._armed.remove(key)
            if self._engaged and self._engaged["key"] == key:
                self._engaged = None
            self._save()
        return await self._publish()

    async def _publish(self) -> dict | None:
        self._published = self.snapshot()
        return await self._deps.publish_state()

    async def _publish_if_changed(self) -> None:
        if self.snapshot() != self._published:
            await self._publish()

    # Monitor loop ----------------------------------------------------------

    async def run(self) -> None:
        while True:
            try:
                await self._tick()
                self._last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if str(exc) != self._last_error:
                    logger.warning("Input auto-select check failed: %s", exc)
                    self._last_error = str(exc)
            await asyncio.sleep(TICK_SECONDS)

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._stop_probes({})

    async def _tick(self) -> None:
        now = self._deps.monotonic()
        if not self._armed:
            await self._stop_probes({})
            self._detectors.clear()
            self._was_active.clear()
            self._set_engaged(None)
            await self._publish_if_changed()
            return
        if self._deps.measurement_owns_graph():
            # Measurement owns the inputs and the graph rate: no captures,
            # no switches. The pause does not count as silence.
            if not self._paused:
                self._paused = True
                await self._stop_probes({})
            await self._publish_if_changed()
            return
        if self._paused:
            self._paused = False
            for detector in self._detectors.values():
                detector.hold_over(now)
        await self._reconcile(now)
        await self._evaluate()
        await self._publish_if_changed()

    async def _reconcile(self, now: float) -> None:
        for key in [key for key in self._detectors if key not in self._armed]:
            del self._detectors[key]
            self._was_active.pop(key, None)
        specs: dict[str, ProbeSpec | None] = {}
        if any(key != STDIN_KEY for key in self._armed):
            overview = self._deps.get_fresh_overview(OVERVIEW_MAX_AGE_SECONDS) or await self._deps.read_overview()
            if now - self._capture_rate_at >= CAPTURE_RATE_REFRESH_SECONDS:
                self._capture_rate = await asyncio.to_thread(self._deps.resolve_capture_rate)
                self._capture_rate_at = now
            entries = {entry.get("key"): entry for entry in overview.get("inputs") or [] if isinstance(entry, dict)}
            specs = {key: self._probe_spec(entries.get(key)) for key in self._armed if key != STDIN_KEY}
        for key in self._armed:
            detector = self._detectors.get(key)
            spec = specs.get(key)
            if detector is None:
                kind = KIND_STDIN if key == STDIN_KEY else (KIND_DIGITAL if spec and spec.digital else KIND_ANALOG)
                engaged = bool(self._engaged and self._engaged["key"] == key)
                detector = self._detectors[key] = ActivityDetector(
                    kind, now, assume_active=engaged,
                    release_seconds=RELEASE_SECONDS if engaged else REARM_SECONDS,
                )
                self._was_active[key] = engaged
            elif spec is not None and spec.digital:
                detector.mark_digital()
        wanted = {key: spec for key, spec in specs.items() if spec is not None}
        await self._stop_probes(wanted)
        for key, spec in wanted.items():
            if key not in self._probes:
                probe = self._deps.probe_factory(spec, self._detectors[key])
                probe.start()
                self._probes[key] = probe
        for key, detector in self._detectors.items():
            if key == STDIN_KEY:
                detector.observe_presence(now, self._stdin_present())
            elif key in self._probes:
                detector.update(now)
            else:
                detector.observe_missing(now)

    def _probe_spec(self, entry: dict[str, Any] | None) -> ProbeSpec | None:
        # Another port of a multi-port source is not what its capture
        # carries right now, so it cannot be monitored.
        if not entry or entry.get("is_active_port") is False or not self._capture_rate:
            return None
        source = str(entry.get("source_key") or entry.get("name") or "").strip()
        left = str(entry.get("left_channel") or "FL").strip()
        right = str(entry.get("right_channel") or "FR").strip()
        if not source or left == right:
            return None
        card = entry.get("alsa_card")
        return ProbeSpec(
            source_name=source,
            left_channel=left,
            right_channel=right,
            rate=int(self._capture_rate),
            card=card if isinstance(card, int) else None,
            digital=_is_digital_entry(entry),
        )

    def _stdin_present(self) -> bool:
        status = self._deps.get_stdin_status() or {}
        return bool(status.get("session_id")) and status.get("state") in _STDIN_PRESENT_STATES

    async def _stop_probes(self, keep: dict[str, ProbeSpec]) -> None:
        for key, probe in list(self._probes.items()):
            if keep.get(key) != probe.spec:
                del self._probes[key]
                await probe.stop()

    # Decisions -------------------------------------------------------------

    async def _evaluate(self) -> None:
        selection = self._deps.load_selection()
        current = selection_key(selection)
        if self._engaged and self._engaged["key"] != current:
            # Selection changed elsewhere (manual choice): it is the new base.
            self._set_engaged(None)
        active = {key: detector.active for key, detector in self._detectors.items()}
        rising = [key for key in self._armed if active.get(key) and not self._was_active.get(key)]
        self._was_active = active
        engaged = self._engaged
        if engaged is not None:
            if not active.get(engaged["key"]):
                await self._release(engaged)
            # Otherwise the engaged input keeps the route; another armed
            # input that is still active takes over once it released.
            return
        for key in rising:
            if key == current:
                continue
            mode = str(selection.get("mode") or SOURCE_MODE_APP_PLAYBACK)
            new_engaged = {
                "key": key,
                "return_mode": mode,
                "return_input_key": (selection.get("selected_input_key") or None)
                if mode == SOURCE_MODE_EXTERNAL_INPUT else None,
            }
            try:
                if await self._switch(current, _target_for_key(key), new_engaged) is not None:
                    logger.info("Input auto-select: signal on %s, switched from %s", key, current)
            except Exception as exc:
                logger.warning("Input auto-select could not switch to %s: %s", key, exc)
            return

    async def _release(self, engaged: dict[str, Any]) -> None:
        plans: list[tuple[Target, dict[str, Any] | None]] = []
        successor = next((key for key in self._armed if key != engaged["key"]
                          and self._detectors.get(key) is not None and self._detectors[key].active), None)
        if successor is not None:
            plans.append((_target_for_key(successor), {**engaged, "key": successor}))
        return_target: Target = (engaged["return_mode"], engaged["return_input_key"])
        plans.append((return_target, None))
        if return_target[0] != SOURCE_MODE_APP_PLAYBACK:
            plans.append(((SOURCE_MODE_APP_PLAYBACK, None), None))
        for target, next_engaged in plans:
            try:
                result = await self._switch(engaged["key"], target, next_engaged)
            except Exception as exc:
                logger.warning("Input auto-select could not switch to %s: %s", target[1] or target[0], exc)
                continue
            if result is not None:
                logger.info("Input auto-select: signal on %s ended, switched to %s",
                            engaged["key"], target[1] or target[0])
            return
        self._set_engaged(None)

    async def _switch(self, expected_key: str, target: Target,
                      next_engaged: dict[str, Any] | None) -> dict | None:
        """Commit ``target`` unless the selection moved away from ``expected_key``.

        The engagement is recorded under the lock before the commit, so the
        commit's own push already carries it; a failed commit restores it.
        """
        previous = self._engaged

        def decide(live_selection: dict) -> Target | None:
            if selection_key(live_selection) != expected_key:
                return None
            if next_engaged is not None and next_engaged["key"] not in self._armed:
                return None  # disarmed while waiting for the lock
            self._set_engaged(next_engaged)
            return target

        try:
            return await self._deps.switch_if(decide)
        except BaseException:
            self._set_engaged(previous)
            raise
