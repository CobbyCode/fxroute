# SPDX-License-Identifier: AGPL-3.0-only
"""Last observed user levels across hardware-sink replacement."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import logging
import threading

from audio import system_volume
from audio.samplerate.parsing import is_hifi_combined_key, successor_sink_key
from common.run_to_completion import run_to_completion, wait_for_completion

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SinkLevels:
    index: int
    volumes: tuple[tuple[str, int], ...]
    muted: bool


class OutputLevels:
    """Observe on the volume monitor; gate replacements during runtime sync."""

    def __init__(self, *, command_runner=None, selected_key=None, mute_held=lambda: False,
                 observation_owner=lambda: None):
        self._run = command_runner or system_volume._run_command
        self._selected_key = selected_key or system_volume._selected_output_name
        self._mute_held = mute_held
        self._observation_owner = observation_owner
        self._observed: dict[str, SinkLevels] = {}
        self._pending: dict[str, SinkLevels] = {}
        self._lock = threading.Lock()
        self._switching = False

    def _inventory(self) -> dict[str, SinkLevels]:
        rows = json.loads(self._run(['pactl', '-f', 'json', 'list', 'sinks']))
        result = {}
        for row in rows:
            volumes = tuple((position, value['value']) for position, value in row['volume'].items())
            if (not volumes or any(type(value) is not int or value < 0 for _, value in volumes)
                    or type(row['mute']) is not bool):
                raise RuntimeError('Hardware output levels are unavailable')
            result[row['name']] = SinkLevels(row['index'], volumes, row['mute'])
        return result

    def observe_selected(self) -> None:
        """Never replace old-node evidence with defaults from its new incarnation."""
        with self._lock:
            if self._switching or self._mute_held():
                return
            key = self._selected_key()
            if not key or key in self._pending:
                return
            owner = self._observation_owner()
            current = self._inventory().get(key)
            previous = self._observed.get(key)
            if (current and not self._mute_held() and owner == self._observation_owner()
                    and key == self._selected_key() and (previous is None or previous.index == current.index)):
                self._observed[key] = current

    def _mute(self, key: str, muted: bool) -> None:
        self._run(['pactl', 'set-sink-mute', key, str(int(muted))])
        if self._inventory()[key].muted != muted:
            raise RuntimeError('Hardware output mute was not confirmed')

    def _begin(self, previous_key: str | None, key: str) -> SinkLevels | None:
        with self._lock:
            if is_hifi_combined_key(key):
                return None
            owner = self._observation_owner()
            live = self._inventory()
            target = live.get(key)
            if target is None:
                raise RuntimeError('Hardware output disappeared before sync')
            previous = self._pending.get(previous_key) or self._observed.get(previous_key)
            replaced = previous is not None and (previous_key not in live or
                (previous_key == key and previous.index != target.index))
            inherited = replaced and successor_sink_key(previous_key, live) == key
            state = self._pending.get(key)
            pending_source = next((levels for source, levels in reversed(self._pending.items())
                                   if successor_sink_key(source, live) == key), None)
            source = state or pending_source or (previous if inherited else None)
            if source is not None:
                old_volumes = dict(source.volumes)
                volumes = tuple((position, old_volumes.get(position, max(old_volumes.values())))
                                for position, _ in target.volumes)
                state = SinkLevels(target.index, volumes, source.muted)
            if state is None and previous_key and previous_key != key:
                # A deliberate different-device selection keeps that device's own levels.
                state = target
            if state is None and self._mute_held():
                # Unknown user intent must never open an owned safety gate.
                user_muted = previous.muted if previous is not None else True
                state = SinkLevels(target.index, target.volumes, user_muted)
            self._switching = True
            if state is None:
                if not self._mute_held() and owner == self._observation_owner():
                    self._observed[key] = target
                return None
            state = SinkLevels(target.index, state.volumes, state.muted)
            self._pending[key] = state
            try:
                self._mute(key, True)
                self._run(['pactl', 'set-sink-volume', key, *(str(value) for _, value in state.volumes)])
                if self._inventory()[key].volumes != state.volumes:
                    raise RuntimeError('Hardware output volume was not confirmed')
            except BaseException:
                self._switching = False
                raise
            return state

    def _finish(self, key: str, state: SinkLevels | None, success: bool) -> None:
        with self._lock:
            try:
                if state is not None and success:
                    live = self._inventory()
                    current = live[key]
                    if (current.index != state.index
                            or tuple(dict(current.volumes)) != tuple(dict(state.volumes))):
                        raise RuntimeError('Hardware output changed during sync')
                    # Raw level edits do not replace the node. Keep a user's new
                    # volume or explicit unmute instead of failing the switch.
                    state = SinkLevels(current.index, current.volumes, state.muted if current.muted else False)
                    self._mute(key, state.muted or self._mute_held())
                    self._observed[key] = state
                    for source in list(self._pending):
                        if successor_sink_key(source, live) == key:
                            self._pending.pop(source)
            except BaseException as error:
                try:
                    self._mute(key, True)
                except Exception as mute_error:
                    logger.exception('Hardware output safety mute failed')
                    error.add_note(f'Safety mute also failed: {mute_error}')
                raise
            finally:
                self._switching = False

    @asynccontextmanager
    async def switch(self, previous_key: str | None, key: str):
        task = asyncio.create_task(asyncio.to_thread(self._begin, previous_key, key))
        cancelled = await wait_for_completion(task)
        state = task.result()
        try:
            if cancelled:
                raise cancelled
            yield
        except BaseException as error:
            try:
                await run_to_completion(asyncio.to_thread(self._finish, key, state, False))
            except BaseException as cleanup_error:
                logger.exception('Hardware output cleanup failed after sync error')
                error.add_note(f'Output level cleanup also failed: {cleanup_error}')
            raise
        else:
            await run_to_completion(asyncio.to_thread(self._finish, key, state, True))
