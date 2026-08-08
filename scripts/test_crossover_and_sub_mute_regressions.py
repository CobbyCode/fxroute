#!/usr/bin/env python3
"""Regression tests: crossover persistence (2.2 readback) and same-mode mute.

Bug 1: A 2.2 crossover edit was persisted to the top-level 2.2 payload, but
`_load_audio_output_mode()` re-derived the global fields from the stale legacy
2.1 `subwoofer` block (or the 80 Hz default when no legacy block existed), so
every readback forced the UI back to 80 Hz.

Bug 2: Every `/api/audio/output-mode` POST went through the Coordinator's
muted `output-mode-switch` transition, closing the hardware-output gate even
for pure DSP parameter changes (level, alignment, polarity, crossover) that
change no routing, samplerate or graph topology.  Same-mode saves must use
the direct persist + `_sync_subwoofer_runtime` path instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import samplerate
import main


def _write_mode_file(config_home: Path, payload: dict) -> None:
    path = Path(config_home) / "fxroute" / "audio-output-mode.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _run_samplerate_roundtrip() -> None:
    """Bug 1: 2.2 readback must honour the top-level crossover field."""
    with tempfile.TemporaryDirectory() as raw:
        config_home = Path(raw)
        old_home = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(config_home)
        try:
            # Fresh 2.2 payload (no legacy subwoofer block): the readback used
            # to fall back to the 80 Hz default because fallback_21 was None.
            fresh = {
                "mode": "subwoofer-2.2",
                "crossover_frequency_hz": 150,
                "slope": "LR24",
                "main_highpass_enabled": True,
                "subwoofers": {
                    "sub1": {"level_db": 0.0, "alignment_ms": 0.0, "polarity": "normal"},
                    "sub2": {"level_db": 0.0, "alignment_ms": 0.0, "polarity": "normal"},
                },
            }
            _write_mode_file(config_home, fresh)
            loaded = samplerate._load_audio_output_mode()
            assert loaded["crossover_frequency_hz"] == 150, loaded
            assert loaded["main_highpass_enabled"] is True, loaded

            # Stale legacy 2.1 block: the readback used to derive 80 from it.
            with_legacy = dict(fresh)
            with_legacy["subwoofer"] = {
                "crossover_frequency_hz": 80,
                "slope": "LR24",
                "main_highpass_enabled": False,
                "sub_level_db": 6.5,
                "sub_alignment_ms": -0.98,
                "sub_polarity": "invert",
            }
            _write_mode_file(config_home, with_legacy)
            loaded = samplerate._load_audio_output_mode()
            assert loaded["crossover_frequency_hz"] == 150, loaded
            assert loaded["main_highpass_enabled"] is True, loaded

            # 2.2 save keeps the legacy block's global fields in sync, so a
            # later 2.1 migration inherits the edited crossover.
            built = samplerate._build_audio_output_mode_payload(
                "subwoofer-2.2",
                {
                    "crossover_frequency_hz": 150,
                    "main_highpass_enabled": True,
                    "sub_level_db": -3.0,
                    "sub_alignment_ms": 1.2,
                    "sub_polarity": "normal",
                },
                None,
            )
            assert built["crossover_frequency_hz"] == 150, built
            assert built["subwoofer"]["crossover_frequency_hz"] == 150, built
            samplerate.persist_audio_output_mode(built)
            migrated_21 = samplerate._build_audio_output_mode_payload(
                "subwoofer-2.1", None, None
            )
            assert migrated_21["subwoofer"]["crossover_frequency_hz"] == 150, migrated_21

            # 2.1 round-trip must stay unaffected.
            samplerate._save_audio_output_mode(
                "subwoofer-2.1",
                {
                    "crossover_frequency_hz": 120,
                    "main_highpass_enabled": False,
                    "sub_level_db": -3.0,
                    "sub_alignment_ms": 1.2,
                    "sub_polarity": "invert",
                },
                None,
            )
            loaded = samplerate._load_audio_output_mode()
            assert loaded["subwoofer"]["crossover_frequency_hz"] == 120, loaded
        finally:
            if old_home is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_home
    print("crossover persistence round-trip: ok")


class FakeRequest:
    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


def _target(mode: str) -> dict:
    return {
        "overview": {"output_mode": {"mode": mode}},
        "config": {"mode": mode, "subwoofer": {}},
    }


def _route_patch_context(*, current_mode: str, target_mode: str):
    """Return (exit_stack, sync, run, set_mode) with the route fully mocked."""
    sync = mock.AsyncMock()
    run = mock.AsyncMock(return_value=mock.MagicMock(committed=True))
    set_mode = mock.MagicMock(return_value={"output_mode": {"mode": target_mode}, "selected_output": None})
    stack = mock.patch.multiple(
        main,
        measurement_sr_session=mock.MagicMock(has_active_jobs=False),
        prepare_audio_output_mode=mock.MagicMock(return_value=_target(target_mode)),
        persist_audio_output_mode=set_mode,
        _sync_subwoofer_runtime=sync,
        _with_subwoofer_derived_delays=lambda value: value,
        subwoofer_runtime=None,
        refresh_peak_monitor_after_effects_change=mock.AsyncMock(),
        _coordinator_current_playback_context=mock.AsyncMock(return_value={
            "source": "local", "target_url": None, "target_track": {}, "should_play": False,
        }),
        get_samplerate_status=mock.MagicMock(return_value={"active_rate": 44100}),
        _run_coordinated_transition=run,
        get_audio_output_overview=mock.MagicMock(return_value={"output_mode": {"mode": target_mode}}),
    )
    load = mock.patch.object(
        main.samplerate,
        "_load_audio_output_mode",
        return_value={"mode": current_mode},
    )
    return stack, load, sync, run, set_mode


async def _route_same_mode_direct() -> None:
    """Bug 2: same-mode DSP edit must bypass the Coordinator/gate entirely."""
    stack, load, sync, run, set_mode = _route_patch_context(
        current_mode="subwoofer-2.2-stereo", target_mode="subwoofer-2.2-stereo"
    )
    with stack, load:
        result = await main.save_audio_output_mode_route(FakeRequest({
            "mode": "subwoofer-2.2-stereo",
            "subwoofer": {"crossover_frequency_hz": 150},
        }))
    run.assert_not_awaited()
    sync.assert_awaited_once()
    set_mode.assert_called_once()
    assert result["output_mode"]["mode"] == "subwoofer-2.2-stereo"
    print("same-mode route uses direct sync (no gate): ok")


async def _route_mode_switch_coordinated() -> None:
    """A real mode switch still needs the muted Coordinator transition."""
    stack, load, sync, run, set_mode = _route_patch_context(
        current_mode="stereo", target_mode="subwoofer-2.1"
    )
    with stack, load:
        await main.save_audio_output_mode_route(FakeRequest({
            "mode": "subwoofer-2.1",
            "subwoofer": {"crossover_frequency_hz": 150},
        }))
    run.assert_awaited_once()
    sync.assert_not_awaited()
    set_mode.assert_not_called()
    request = run.await_args.args[0]
    assert request.operation == "output-mode-switch"
    print("mode-switch route keeps coordinated transition: ok")


async def main_async() -> None:
    _run_samplerate_roundtrip()
    await _route_same_mode_direct()
    await _route_mode_switch_coordinated()
    print("crossover / sub mute regression tests: ok")


if __name__ == "__main__":
    asyncio.run(main_async())
