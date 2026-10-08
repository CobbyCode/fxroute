# SPDX-License-Identifier: AGPL-3.0-only
"""Exclusive Level Matching captures and temporary untrimmed plan restoration."""

from __future__ import annotations

import copy
import math
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from audio.output_routing import device_key
from common.run_to_completion import run_to_completion
from dsp.runtime import OUTPUT_GAIN_TOLERANCE_DB


def build_level_match_scope(*, output_service: Any, get_session: Callable,
                            get_native_runtime: Callable, build_target: Callable,
                            prepare_measurement: Callable):
    """Own every take; neutralize unequal way trims without committing output state."""

    @asynccontextmanager
    async def owned(job: dict, target: dict, scope: dict, state: dict, *, neutralize: bool):
        session = get_session()
        if session is None:
            raise RuntimeError("Level Matching untrimmed capture requires a measurement session")
        epoch = job["_entry_epoch"]
        registered = False
        staged = False
        native = None
        original = None
        gain = None

        def require_owner() -> None:
            if (output_service.load()["revision"] != state["revision"]
                    or target["revision"] != state["revision"]):
                raise RuntimeError("Level Matching output state changed; run again")
            if native is not None and get_native_runtime() is not native:
                raise RuntimeError("Level Matching native runtime ownership changed")

        async def verify(rendered) -> None:
            require_owner()
            before = native.snapshot()
            links_valid = await native.verify()
            require_owner()
            after = native.snapshot()
            config = after.get("config") or {}
            if (not links_valid or before.get("active") is not True or after.get("active") is not True
                    or not after.get("helper_pid") or before.get("helper_pid") != after.get("helper_pid")
                    or before.get("config") != config
                    or config.get("plan_fingerprint") != rendered.config.plan_fingerprint
                    or config.get("sample_rate") != target["sample_rate_hz"]
                    or config.get("output_key") != rendered.config.output_key
                    or config.get("hardware_ports") != list(rendered.config.hardware_ports)):
                raise RuntimeError("Level Matching runtime plan could not be verified")

        async def transition(new, previous) -> None:
            require_owner()
            await native.guarded_rebuild_rendered(
                new, previous=previous, guard_db=min(-30.0, gain),
                ramp_target_db=gain, rollback_ramp_target_db=gain,
                apply_candidate=require_owner, apply_previous=require_owner,
                before_ramp=lambda: verify(new), before_rollback_ramp=lambda: verify(previous))
            await verify(new)
            if abs(await native.read_output_gain_db() - gain) > OUTPUT_GAIN_TOLERANCE_DB:
                raise RuntimeError("Level Matching output gain was not restored")
            require_owner()

        async def teardown() -> None:
            safe_to_release = True
            try:
                if staged:
                    # A failed restoration must never roll back to the temporary plan.
                    try:
                        await transition(original, original)
                    except Exception as restore_error:
                        safe_to_release = False
                        try:
                            if get_native_runtime() is not native:
                                raise RuntimeError("Native runtime ownership changed")
                            await native.stop_verified()
                            snapshot = native.snapshot()
                            if snapshot.get("active") is not False or snapshot.get("helper_pid"):
                                raise RuntimeError("Native runtime did not stop")
                            safe_to_release = True
                        except Exception as stop_error:
                            raise RuntimeError(
                                "Level Matching restore and shutdown failed; recovery required: "
                                f"{restore_error}; {stop_error}") from restore_error
                        raise RuntimeError(
                            f"Level Matching restore failed; native runtime stopped: {restore_error}") from restore_error
                elif original is not None:
                    await verify(original)
                    if abs(await native.read_output_gain_db() - gain) > OUTPUT_GAIN_TOLERANCE_DB:
                        raise RuntimeError("Level Matching output gain changed during the run")
                    require_owner()
            finally:
                if registered and safe_to_release:
                    await session.unregister_speaker_job(job["id"])

        try:
            require_owner()
            _, entered = await session.register_speaker_job(job["id"], entry_epoch=epoch)
            registered = True
            await prepare_measurement(target["sample_rate_hz"], graph_already_verified=entered)
            native = get_native_runtime()
            if native is None:
                raise RuntimeError("Level Matching native DSP runtime is unavailable")
            require_owner()
            snapshot = native.snapshot()
            config = snapshot.get("config") or {}
            output_key = config.get("output_key")
            ports = config.get("hardware_ports") or []
            if (device_key(output_key) != target["device_key"] or len(ports) < target["channels"]):
                raise RuntimeError("Level Matching output device changed")
            gain = await native.read_output_gain_db()
            require_owner()
            if type(gain) not in (int, float) or not math.isfinite(gain) or not -80 <= gain <= 0:
                raise RuntimeError("Level Matching output gain readback is unavailable")

            def render(document: dict):
                plan = output_service.compile_plan(
                    document, output_key=output_key, channels=target["channels"],
                    sample_rate_hz=target["sample_rate_hz"])
                return build_target(plan, output_key=output_key, hardware_ports=ports,
                                    fingerprint=output_service.fingerprint_plan(plan))

            original = render(state)
            await verify(original)
            neutral = copy.deepcopy(state)
            untrimmed = original
            if neutralize:
                processing = neutral["modes"][target["mode"]]["processing"]
                for unit in scope["units"]:
                    for role in unit["roles"]:
                        processing[role]["level_cal_db"][unit.get("slot", scope["slot"])] = 0.0
                        if target["mode"] != "surround":
                            processing[role]["level_cal_db"]["bank"] = 0.0
                untrimmed = render(neutral)
                staged = True
                await transition(untrimmed, original)
            capture_target = copy.deepcopy(target)
            capture_target["processing_fingerprint"] = untrimmed.config.plan_fingerprint
            yield {"state": neutral, "target": capture_target,
                   "expected_native_context": {
                       "expected_native_layout": [dict(row) for row in untrimmed.config.layout],
                       "expected_native_output_mode": untrimmed.config.output_mode,
                       "expected_plan_fingerprint": untrimmed.config.plan_fingerprint},
                   "session_owned": True}
        finally:
            await run_to_completion(teardown())

    return owned
