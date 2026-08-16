# SPDX-License-Identifier: AGPL-3.0-only

"""Output-gate ownership, persistence, and startup reconciliation.

Extracted from :class:`PlaybackTransitionCoordinator` so the hardware-output
gate contract lives in one cohesive module.  The mixin runs on the composing
coordinator instance and reads the attributes declared on the class below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .models import DSP_TRANSPORT_SINK, OutputGateState
from .protocol import TransitionRuntime

logger = logging.getLogger(__name__)


class _OutputGateMixin:
    """Attributes provided by the composing coordinator instance."""
    gate: OutputGateState
    runtime: TransitionRuntime
    gate_settle_seconds: float
    gate_state_path: Path | None
    lock: asyncio.Lock
    _startup_gate_reconciled: bool
    _startup_gate_error: str | None

    def _persist_gate_state(self) -> None:
        """Persist ownership before muting so a restart can resolve stale state."""
        if self.gate_state_path is None:
            return
        path = self.gate_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        payload = {"version": 1, "gate": self.gate.as_dict()}
        try:
            temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_gate_state(self) -> dict[str, Any] | None:
        if self.gate_state_path is None:
            return None
        try:
            payload = json.loads(self.gate_state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, TypeError, ValueError):
            self.gate_state_path.unlink(missing_ok=True)
            return None
        state = payload.get("gate") if isinstance(payload, dict) else None
        return state if isinstance(state, dict) else None

    def _clear_gate_state(self) -> None:
        if self.gate_state_path is None:
            return
        try:
            self.gate_state_path.unlink(missing_ok=True)
        except OSError:
            # The in-memory gate is still authoritative for this process; a
            # stale marker is harmless because startup reconciliation restores
            # the same user-mute value before clearing it on the next start.
            pass

    async def _reconcile_startup_gate_locked(self) -> bool:
        if self._startup_gate_reconciled:
            return True
        persisted = self._load_gate_state()
        if not persisted or persisted.get("owner") != "fxroute" or not persisted.get("closed"):
            self._clear_gate_state()
            try:
                await self._ensure_dsp_sink_unmuted(
                    "startup-stale-mute-reconcile"
                )
                observed_muted = await self.runtime.read_hardware_mute()
                if observed_muted:
                    await self.runtime.set_hardware_mute(
                        False, "startup-stale-mute-reconcile"
                    )
                    if await self.runtime.read_hardware_mute():
                        raise RuntimeError(
                            "startup stale hardware mute could not be cleared"
                        )
                    logger.info(
                        "Playback transition startup cleared stale hardware mute"
                    )
            except Exception as exc:
                self._startup_gate_error = str(exc)
                self._startup_gate_reconciled = False
                return False
            self._startup_gate_error = None
            self._startup_gate_reconciled = True
            return True

        transition_id = str(persisted.get("transition_id") or "startup-gate-reconcile")
        original_user_muted = bool(persisted.get("original_user_muted"))
        self.gate = OutputGateState(
            closed=True,
            original_user_muted=original_user_muted,
            owner="fxroute",
            transition_id=transition_id,
            failure_latched=bool(persisted.get("failure_latched")),
            closed_at=persisted.get("closed_at"),
        )
        try:
            await self.runtime.set_hardware_mute(original_user_muted, transition_id)
            if await self.runtime.read_hardware_mute() != original_user_muted:
                raise RuntimeError("startup output-gate restoration was not confirmed")
        except Exception as exc:
            self._startup_gate_error = str(exc)
            self.gate.failure_latched = True
            self.gate.closed = True
            self.gate.owner = "fxroute"
            try:
                self._persist_gate_state()
                await self.runtime.set_hardware_mute(True, transition_id)
            except Exception:
                pass
            return False

        self.gate = OutputGateState()
        self._clear_gate_state()
        self._startup_gate_error = None
        self._startup_gate_reconciled = True
        return True

    async def reconcile_startup_gate(self) -> bool:
        """Resolve an FXRoute-owned mute left by an earlier process."""
        async with self.lock:
            return await self._reconcile_startup_gate_locked()

    async def _read_dsp_sink_mute(self) -> bool | None:
        reader = getattr(self.runtime, "read_sink_mute", None)
        if not callable(reader):
            # Minimal test adapters predating the explicit internal-sink
            # contract have no second physical sink.  The production adapter
            # always implements this readback.
            return None
        try:
            return bool(await reader(DSP_TRANSPORT_SINK))
        except Exception as exc:
            raise RuntimeError(
                "DSP ingress sink mute readback failed: "
                f"{exc}"
            ) from exc

    async def _set_dsp_sink_mute(
        self, muted: bool, transition_id: str
    ) -> None:
        setter = getattr(self.runtime, "set_sink_mute", None)
        if not callable(setter):
            raise RuntimeError(
                "DSP ingress sink mute control is unavailable for an audible transition"
            )
        try:
            await setter(DSP_TRANSPORT_SINK, muted, transition_id)
        except Exception as exc:
            raise RuntimeError(
                "DSP ingress sink mute write failed: "
                f"{exc}"
            ) from exc

    async def _ensure_dsp_sink_unmuted(self, transition_id: str) -> None:
        observed_muted = await self._read_dsp_sink_mute()
        if observed_muted is None:
            return
        if observed_muted:
            await self._set_dsp_sink_mute(False, transition_id)
        readback = await self._read_dsp_sink_mute()
        if readback is not False:
            raise RuntimeError(
                "DSP ingress sink mute could not be confirmed unmuted"
            )

    async def _verify_audible_output_readback(self, stage: str) -> None:
        hardware_muted = bool(await self.runtime.read_hardware_mute())
        if hardware_muted:
            raise RuntimeError(
                f"hardware output remained muted at audible commit boundary: {stage}"
            )
        dsp_sink_muted = await self._read_dsp_sink_mute()
        if dsp_sink_muted is True:
            raise RuntimeError(
                f"DSP ingress sink remained muted at audible commit boundary: {stage}"
            )

    async def _close_gate(
        self, transition_id: str, *, audible_output: bool = False
    ) -> None:
        observed_muted = await self.runtime.read_hardware_mute()
        # A mute left behind by an earlier FXRoute failure is owned by the
        # coordinator, not evidence of a newly user-muted sink.
        if audible_output:
            # Audible play/recovery/measurement-entry is an explicit request
            # for sound.  A stale physical mute must not become the next
            # transition's user intent.
            self.gate.original_user_muted = False
        elif not self.gate.closed:
            self.gate.original_user_muted = bool(observed_muted)
        elif self.gate.failure_latched and not observed_muted:
            # The user explicitly unmuted after the failure; begin a fresh
            # ownership interval without carrying the old latch forward.
            self.gate.original_user_muted = False
            self.gate.failure_latched = False

        self.gate.closed = True
        self.gate.owner = "fxroute"
        self.gate.transition_id = transition_id
        self.gate.closed_at = time.monotonic()
        self._persist_gate_state()
        await self.runtime.set_hardware_mute(True, transition_id)
        if not await self.runtime.read_hardware_mute():
            raise RuntimeError("hardware output gate could not be confirmed closed")
        if audible_output:
            await self._ensure_dsp_sink_unmuted(transition_id)

    async def ensure_output_gate_closed(
        self,
        transition_id: str,
        *,
        stage: str,
    ) -> None:
        """Confirm the physical sink mute while this transition owns the gate.

        The in-memory state is only an ownership record.  Every critical
        boundary reads the actual sink state and repairs a lost mute once.  A
        failed readback is fatal so a transition can never proceed on the
        assumption that an output gate is still closed.
        """
        if (
            not self.gate.closed
            or self.gate.owner != "fxroute"
            or self.gate.transition_id != transition_id
        ):
            raise RuntimeError(
                f"hardware output gate ownership missing at {stage}"
            )
        try:
            muted = bool(await self.runtime.read_hardware_mute())
        except Exception as exc:
            raise RuntimeError(
                f"hardware output gate readback failed at {stage}: {exc}"
            ) from exc
        if muted:
            return

        try:
            await self.runtime.set_hardware_mute(True, transition_id)
            muted = bool(await self.runtime.read_hardware_mute())
        except Exception as exc:
            raise RuntimeError(
                f"hardware output gate re-mute failed at {stage}: {exc}"
            ) from exc
        if not muted:
            raise RuntimeError(
                f"hardware output gate could not be confirmed closed at {stage}"
            )

    async def _hold_gate_after_verification(self) -> None:
        if self.gate.closed and self.gate_settle_seconds:
            await asyncio.sleep(self.gate_settle_seconds)

    async def _restore_gate(
        self,
        transition_id: str,
        *,
        audible_output: bool = False,
        after_physical_restore: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not self.gate.closed:
            return
        if self.gate.transition_id != transition_id or self.gate.owner != "fxroute":
            raise RuntimeError("hardware output gate ownership changed during transition")
        restore_muted = False if audible_output else bool(self.gate.original_user_muted)
        if audible_output:
            await self._ensure_dsp_sink_unmuted(transition_id)
        await self.runtime.set_hardware_mute(restore_muted, transition_id)
        if (await self.runtime.read_hardware_mute()) != restore_muted:
            raise RuntimeError("hardware output gate restoration was not confirmed")
        if after_physical_restore is not None:
            await after_physical_restore()
        if audible_output:
            await self._verify_audible_output_readback("before-gate-open")
        self.gate.closed = False
        self.gate.owner = None
        self.gate.transition_id = None
        self.gate.closed_at = None
        self.gate.failure_latched = False
        self.gate.original_user_muted = None
        self._clear_gate_state()
        if audible_output:
            await self._verify_audible_output_readback("after-gate-open")
            logger.info(
                "Playback transition audible sink readback: fxroute_dsp_sink_muted=False "
                "hardware_muted=False gate.closed=%s",
                self.gate.closed,
            )

    async def _latch_failure(self, transition_id: str) -> None:
        self.gate.failure_latched = True
        self.gate.closed = True
        self.gate.owner = "fxroute"
        self.gate.transition_id = transition_id
        try:
            self._persist_gate_state()
        except Exception:
            pass
        try:
            await self.runtime.set_hardware_mute(True, transition_id)
        except Exception:
            # The original transition error remains authoritative; the
            # structured status still records the output gate as latched.
            pass

