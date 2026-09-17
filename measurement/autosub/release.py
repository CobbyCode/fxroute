# SPDX-License-Identifier: AGPL-3.0-only

"""Release-runtime adapter: rebuild the committed plan at session release.

After an AutoSub service run commits its winner, the persisted legacy mode
file still shows the frozen start (service jobs never persist legacy).  A
session release that re-syncs the runtime from the legacy overview would
rebuild the stale graph and audibly lose the commit.  The adapter built here
renders the *current* ``OutputService.load()`` at the restore rate and syncs
the prebuilt target (the output-state equivalent of the legacy
``dsp_runtime.sync(overview)``); the measurement session invokes it instead
of the legacy overview sync.  Rendering is always live, never rebased: a
later concurrent writer is consumed as-is.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from dsp.runtime import DSPRuntimeConfig, PlannedSyncTarget


def _validate_rate(sample_rate_hz: int) -> None:
    if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
        raise ValueError("AutoSub release rate must be a positive integer")


def create_release_adapter(
    *,
    service: Any,
    dsp_manager: Any,
    hardware_ports: list,
    get_native_runtime: Callable[[], Any],
    output_key: str,
    channels: int,
) -> Callable[[int], Awaitable[dict]]:
    """Build the release adapter for one committed AutoSub device context.

    ``hardware_ports`` are discovery data (the selected sink's playback
    ports); ``get_native_runtime`` resolves the live native runtime at
    invoke time.  All device context is validated eagerly so a miswired
    finalizer fails before the session unregister, not mid-release.
    """
    if not isinstance(output_key, str) or not output_key:
        raise ValueError("AutoSub release requires a non-empty output key")
    if type(channels) is not int or channels <= 0:
        raise ValueError("AutoSub release requires a positive integer channel count")
    ports = list(hardware_ports or [])
    if len(ports) < channels:
        raise ValueError("AutoSub release has insufficient discovered playback ports")
    if not callable(getattr(dsp_manager, "compile_engine_text", None)):
        raise ValueError("AutoSub release requires a DSP manager")
    if not callable(get_native_runtime):
        raise ValueError("AutoSub release requires a native runtime accessor")

    async def adapter(restore_rate_hz: int) -> dict:
        _validate_rate(restore_rate_hz)
        state = service.load()
        plan = service.compile_plan(
            state, output_key=output_key, channels=channels,
            sample_rate_hz=restore_rate_hz)
        fingerprint = service.fingerprint_plan(plan)
        layout = service.compile_layout(plan)
        config = DSPRuntimeConfig.from_plan(
            plan, layout=layout, output_key=output_key,
            sample_rate_hz=restore_rate_hz, hardware_ports=list(ports),
            plan_fingerprint=fingerprint)
        text = dsp_manager.compile_engine_text(
            [dict(entry) for entry in layout],
            preset_name=plan["global"]["preset"],
            sample_rate_hz=restore_rate_hz,
            extras_override=plan["global"]["extras"])
        native_runtime = get_native_runtime()
        if native_runtime is None:
            raise RuntimeError("Native DSP runtime is unavailable for release rebuild")
        await native_runtime.sync_rendered(PlannedSyncTarget(config, text))
        return {"revision": state["revision"], "plan_fingerprint": fingerprint,
                "sample_rate_hz": restore_rate_hz}

    return adapter
