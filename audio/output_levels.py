# SPDX-License-Identifier: AGPL-3.0-only
"""Last observed user levels across hardware-sink replacement."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
import json
import logging
import threading
from pathlib import Path

from audio import system_volume
from audio.samplerate.parsing import is_hifi_combined_key, successor_sink_key
from common.run_to_completion import run_to_completion, wait_for_completion
from common.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SinkLevels:
    index: int
    volumes: tuple[tuple[str, int], ...]
    muted: bool


class OutputLevels:
    """Observe on the volume monitor; gate replacements during runtime sync."""

    def __init__(self, *, command_runner=None, selected_key=None, mute_held=lambda: False,
                 observation_owner=lambda: None, state_path=None):
        self._run = command_runner or system_volume._run_command
        self._selected_key = selected_key or system_volume._selected_output_name
        self._mute_held = mute_held
        self._observation_owner = observation_owner
        self._observed: dict[str, SinkLevels] = {}
        self._pending: dict[str, SinkLevels] = {}
        self._lock = threading.Lock()
        self._switching = False
        self._state_path = state_path
        self._pending_cookie: str | None = None

    def _marker_path(self) -> Path | None:
        path = self._state_path() if callable(self._state_path) else self._state_path
        return Path(path) if path is not None else None

    def _server_cookie(self) -> str:
        cookie = json.loads(self._run(['pactl', '-f', 'json', 'info'])).get('cookie')
        if not isinstance(cookie, str) or not cookie:
            raise RuntimeError('Audio server identity is unavailable')
        return cookie

    def _persist_pending(self) -> None:
        path = self._marker_path()
        if path is None:
            return
        if not self._pending:
            self._pending_cookie = None
            path.unlink(missing_ok=True)
            return
        cookie = self._server_cookie()
        if self._pending_cookie is not None and cookie != self._pending_cookie:
            self._discard_server_ownership()
            raise RuntimeError('Audio server changed during output sync')
        self._pending_cookie = cookie
        atomic_write_text(path, json.dumps({'version': 1, 'owner': 'fxroute-output-switch',
            'server_cookie': cookie, 'sinks': {key: asdict(state) for key, state in self._pending.items()}}))

    def _discard_server_ownership(self) -> None:
        self._pending.clear()
        self._observed.clear()
        self._pending_cookie = None
        path = self._marker_path()
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning('Could not remove stale output-switch ownership', exc_info=True)

    def _persist_after_mute(self) -> None:
        """Keep live safety/intent authoritative when marker storage fails."""
        try:
            self._persist_pending()
        except OSError:
            logger.warning('Could not persist output-switch ownership', exc_info=True)

    def _check_server_ownership(self) -> None:
        if self._pending_cookie is not None and self._server_cookie() != self._pending_cookie:
            self._discard_server_ownership()

    def user_mute_for(self, key: str) -> bool | None:
        """Transfer owned user intent to a coordinator gate, never its temporary mute."""
        with self._lock:
            self._check_server_ownership()
            state = self._pending.get(key)
            if state is None:
                return None
            current = self._inventory().get(key)
            if current is None or current.index != state.index:
                return None
            return state.muted if current.muted else False

    def recover_pending(self, *, mute_restored: bool = False) -> None:
        """Load owned mutes; only a later verified graph may release them.

        A coordinator gate restored at startup is newer authority. Never
        override it, an explicit unmute, or another server/node incarnation.
        """
        with self._lock:
            path = self._marker_path()
            if path is None:
                return
            try:
                if not path.exists():
                    return
                try:
                    payload = json.loads(path.read_text(encoding='utf-8'))
                    if (not isinstance(payload, dict) or payload.get('version') != 1
                            or payload.get('owner') != 'fxroute-output-switch'
                            or not isinstance(payload.get('sinks'), dict)):
                        raise ValueError('Invalid output-switch ownership marker')
                    saved = {}
                    for key, row in payload['sinks'].items():
                        volumes = tuple((position, value) for position, value in row['volumes'])
                        if (type(row['index']) is not int or type(row['muted']) is not bool
                                or not volumes or any(not isinstance(p, str) or type(v) is not int or v < 0
                                                      for p, v in volumes)):
                            raise ValueError('Invalid output-switch levels')
                        saved[key] = SinkLevels(row['index'], volumes, row['muted'])
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning('Ignoring invalid output-switch ownership: %s', exc)
                    path.unlink(missing_ok=True)
                    return
                cookie = self._server_cookie()
                pending = {}
                if not mute_restored and payload.get('server_cookie') == cookie:
                    live = self._inventory()
                    for key, state in saved.items():
                        current = live.get(key)
                        # Changed raw levels revoke uncertain crash ownership.
                        if (current and current.index == state.index and current.muted
                                and dict(current.volumes) == dict(state.volumes)):
                            pending[key] = state
                self._pending = pending
                self._pending_cookie = cookie if pending else None
                self._persist_pending()
            except Exception as exc:
                logger.warning('Output-switch recovery deferred; retaining ownership marker: %s', exc)

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
            self._check_server_ownership()
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
            previous_pending = self._pending.get(key)
            previous_cookie = self._pending_cookie
            self._pending[key] = state
            try:
                try:
                    self._persist_pending()
                except BaseException:
                    # No mute was acquired. Do not let a failed marker capture
                    # override later user edits, or resurrect discarded servers.
                    if self._pending.get(key) is state:
                        if previous_pending is None:
                            self._pending.pop(key)
                        else:
                            self._pending[key] = previous_pending
                        self._pending_cookie = previous_cookie
                    raise
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
            cookie = self._pending_cookie
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
                    self._pending[key] = state
                    self._persist_after_mute()
                    self._mute(key, state.muted or self._mute_held())
                    self._observed[key] = state
                    for source in list(self._pending):
                        if successor_sink_key(source, live) == key:
                            self._pending.pop(source)
                    self._persist_after_mute()
            except BaseException as error:
                try:
                    current = self._inventory()[key]
                    server_changed = cookie is not None and self._server_cookie() != cookie
                    if server_changed or (state is not None and current.index != state.index):
                        if server_changed:
                            self._discard_server_ownership()
                            replacement = current
                        else:
                            volumes = dict(state.volumes)
                            replacement = SinkLevels(current.index,
                                tuple((position, volumes.get(position, max(volumes.values())))
                                      for position, _ in current.volumes), state.muted)
                        self._pending[key] = replacement
                except Exception as ownership_error:
                    logger.exception('Hardware output safety ownership readback failed')
                    error.add_note(f'Safety ownership readback also failed: {ownership_error}')
                try:
                    self._mute(key, True)
                except Exception as mute_error:
                    logger.exception('Hardware output safety mute failed')
                    error.add_note(f'Safety mute also failed: {mute_error}')
                try:
                    self._persist_pending()
                except Exception as marker_error:
                    logger.exception('Hardware output safety ownership persistence failed')
                    error.add_note(f'Safety ownership persistence also failed: {marker_error}')
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
