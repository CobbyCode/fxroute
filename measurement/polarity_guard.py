# SPDX-License-Identifier: AGPL-3.0-only
"""Whether the rendered DSP path leaves the Polarity Check's measured band readable.

The detector reads the carrier phase of the 4-14 kHz pulse, so a stage that
rotates the phase there makes it follow the filter instead of the wiring. The
check sweeps through the whole active chain, so this module models what the
detector would read behind a flat, ideal speaker at the rendered path of one
unit: the Global stages that reach its input, and per role the route, level,
delay, polarity, crossover, area-bank PEQ and convolver. The model is read with
the detector's own phase reader.

Only stages known to leave the phase alone, or modelled here, are allowed; any
other active stage makes the path unknown. A guard result never upgrades a
verdict: it only withholds a correct/reversed reading.

Pure analysis: no hardware, no runtime, no files except the IR files a
convolver names.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from dsp.native_model import native_peq_coefficients
from measurement import polarity

logger = logging.getLogger(__name__)

OK = "ok"
ROTATES = "rotates"
UNKNOWN = "unknown"
REASON_FILTER_IN_BAND = "dsp_filter_in_band"
REASON_UNKNOWN = "dsp_unknown"
REASONS = {ROTATES: REASON_FILTER_IN_BAND, UNKNOWN: REASON_UNKNOWN}
# The modelled carrier phase must stay within about 37 degrees of 0 or 180. A half-sample grid
# offset of the detector adds about 38 degrees, so the two cannot flip a sign (75 < 90).
GUARD_MIN_COS = 0.8
# The model IR spans this many seconds, with the response shifted by LEAD_SECONDS so the
# pre-ringing of a filter sits inside the window like the sweep pre-roll of a real take.
MODEL_SECONDS = 2.0
LEAD_SECONDS = 0.2
# Stages that scale or delay the signal and leave its phase alone.
NEUTRAL_GLOBAL_STAGES = frozenset({"delay", "headroom", "autogain", "limiter"})
# Global PEQ band types (LSP names) and their native biquad kind.
_GLOBAL_BAND_TYPES = {
    "bell": "bell", "notch": "notch", "low_pass": "lowpass", "high_pass": "highpass",
    "low_shelf": "lowshelf", "high_shelf": "highshelf",
}
_SHAPING_KINDS = frozenset({"bell", "lowshelf", "highshelf"})
# Surround program positions (native_dsp/dsp.c) and the speaker group each one belongs to.
_POSITION_GROUP = {0: "front", 1: "front", 2: "center", 4: "side", 5: "side", 6: "rear", 7: "rear"}
_POSITION_SIDE = {0: "left", 1: "right", 4: "left", 5: "right", 6: "left", 7: "right"}


class PathUnavailable(Exception):
    """The rendered path of the unit cannot be established; ``stage`` says why."""

    def __init__(self, stage: str):
        super().__init__(stage)
        self.stage = stage


class _Unmodelled(Exception):
    def __init__(self, stage: str):
        super().__init__(stage)
        self.stage = stage


@dataclass(frozen=True)
class _Stage:
    name: str
    response: np.ndarray


@dataclass(frozen=True)
class _RolePath:
    base: np.ndarray
    stages: tuple[_Stage, ...]


def unknown_result(stage: str) -> dict:
    return {"state": UNKNOWN, "dsp_cos": None, "stage": stage}


def assess_unit(config: Mapping[str, Any], unit: Mapping[str, Any], *, sample_rate_hz: int) -> dict:
    """Judge the rendered path of one polarity unit.

    ``config`` is the engine configuration of the rendered plan (``outputs`` and
    the Global ``chain``). Returns ``state`` (ok / rotates / unknown), ``dsp_cos``
    (carrier phase cosine the detector would read behind an ideal speaker, signed)
    and ``stage`` (the stage that rotates or cannot be modelled, else "").
    """
    try:
        return _assess(config, unit, sample_rate_hz)
    except _Unmodelled as exc:
        return unknown_result(exc.stage)
    except (ArithmeticError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Polarity guard could not model the DSP path: %s", exc)
        return unknown_result("DSP path")


def _assess(config: Mapping[str, Any], unit: Mapping[str, Any], rate: int) -> dict:
    if type(rate) is not int or rate <= 0:
        raise ValueError("Sample rate must be a positive integer")
    outputs = {output["name"]: output for output in config["outputs"]}
    roles = list(unit["roles"])
    if not roles or any(role not in outputs for role in roles):
        raise _Unmodelled("unit roles")
    surround = any("input_positions" in output for output in outputs.values())
    size = 1 << math.ceil(math.log2(MODEL_SECONDS * rate))
    omega = 2.0 * np.pi * np.arange(size // 2 + 1) / size
    z1 = np.exp(-1j * omega)
    grid = _Grid(rate=rate, size=size, omega=omega, z1=z1, z2=z1 * z1)
    chain = [plugin for plugin in config.get("chain", []) if plugin.get("enabled", True)]
    # The Global stages of a centre or LFE input run on a pair whose channel is not named here;
    # both sides are modelled and the less readable one counts.
    ambiguous = any(_driven_input(outputs[role]) in (2, 3) for role in roles)
    sides = ("left", "right") if ambiguous else (None,)
    worst: dict | None = None
    for side in sides:
        paths = [_role_path(outputs[role], role, chain, grid, surround, side) for role in roles]
        reading = _read(paths, grid, removed=None)
        if worst is None or abs(reading) < abs(worst["cos"]):
            worst = {"cos": reading, "paths": paths}
    assert worst is not None
    cos = worst["cos"]
    if abs(cos) >= GUARD_MIN_COS:
        return {"state": OK, "dsp_cos": round(cos, 3), "stage": ""}
    return {"state": ROTATES, "dsp_cos": round(cos, 3), "stage": _culprit(worst["paths"], grid)}


@dataclass(frozen=True)
class _Grid:
    rate: int
    size: int
    omega: np.ndarray
    z1: np.ndarray
    z2: np.ndarray


def _driven_input(output: Mapping[str, Any]) -> int:
    """Engine input the sweep excites for this output: its first route (the speaker's own position)."""
    routes = output.get("routes")
    if not isinstance(routes, list) or not routes:
        raise _Unmodelled("input routes")
    return int(routes[0]["input"])


def _biquad(grid: _Grid, coefficients: tuple[float, ...]) -> np.ndarray:
    b0, b1, b2, a1, a2 = coefficients
    return (b0 + b1 * grid.z1 + b2 * grid.z2) / (1.0 + a1 * grid.z1 + a2 * grid.z2)


def _role_path(output: Mapping[str, Any], role: str, chain: list[dict], grid: _Grid,
               surround: bool, side: str | None) -> _RolePath:
    if output.get("post_crossover_routes") or output.get("crossover_groups") or output.get("crossover_sos"):
        raise _Unmodelled(f"{role} bass bus")
    source = _driven_input(output)
    gain = float(output["routes"][0].get("gain", 1.0))
    level = 10.0 ** (float(output.get("gain_db", 0.0)) / 20.0)
    # The engine rounds the output delay to whole samples.
    delay_samples = math.floor(float(output.get("delay_ms", 0.0)) * grid.rate / 1000.0 + 0.5)
    base = (gain * level * (-1.0 if output.get("invert") else 1.0)) * np.exp(-1j * grid.omega * delay_samples)
    stages = [_Stage(name, response) for name, response in _global_stages(chain, source, grid, surround, side)]
    stages.extend(_output_stages(output, role, grid))
    return _RolePath(base=base, stages=tuple(stages))


def _output_stages(output: Mapping[str, Any], role: str, grid: _Grid) -> list[_Stage]:
    """Crossover, area-bank PEQ and convolver of one output."""
    stages = []
    sections = output.get("sos") or []
    if sections:
        response = np.ones_like(grid.z1)
        for section in sections:
            response = response * _biquad(grid, tuple(float(value) for value in section))
        stages.append(_Stage(f"{role} crossover", response))
    bands = output.get("filters") or []
    if bands:
        response = np.ones_like(grid.z1)
        for band in bands:
            frequency = float(band["frequency_hz"])
            if not 0.0 < frequency < grid.rate / 2:
                raise _Unmodelled(f"{role} PEQ")
            coefficients = native_peq_coefficients(
                str(band["type"]), frequency, grid.rate, float(band["q"]), float(band["gain_db"]))
            response = response * _biquad(grid, coefficients) ** int(band.get("stages", 1))
        stages.append(_Stage(f"{role} PEQ", response))
    if output.get("oconv"):
        raise _Unmodelled(f"{role} convolver")
    return stages


def _global_stages(chain: list[dict], source: int, grid: _Grid, surround: bool,
                   side: str | None) -> list[tuple[str, np.ndarray]]:
    """Responses of the enabled Global plugins that act on one engine input."""
    group = _POSITION_GROUP.get(source) if surround else None
    channel = side or _POSITION_SIDE.get(source) or "left"
    stages = []
    for plugin in chain:
        # A Surround Global plugin runs only on the effect pairs of its speaker group.
        if surround and plugin.get("group") and plugin["group"] != group:
            continue
        kind = plugin.get("type")
        if kind in NEUTRAL_GLOBAL_STAGES:
            continue
        if kind == "equalizer":
            response = _global_equalizer(plugin, channel, grid)
            if response is not None:
                stages.append(("global equalizer", response))
            continue
        if kind == "convolver":
            kernel = str((plugin.get("params") or {}).get("kernel", "")).rsplit("/", 1)[-1]
            raise _Unmodelled(f"global convolver {kernel}".rstrip())
        raise _Unmodelled(f"global {str(kind or 'stage').replace('_', ' ')}")
    return stages


def _global_equalizer(plugin: Mapping[str, Any], channel: str, grid: _Grid) -> np.ndarray | None:
    """Response of a Global PEQ for one channel, or None when it leaves the phase alone."""
    params = plugin.get("params") if isinstance(plugin.get("params"), Mapping) else {}
    if params.get("channelMode") == "dual":
        bands = params.get("leftBands" if channel == "left" else "rightBands") or []
    else:
        bands = params.get("bands") or []
    iir = str(params.get("eqMode", "IIR")).upper() == "IIR"
    response = None
    for band in bands:
        if not isinstance(band, Mapping):
            raise _Unmodelled("global equalizer")
        if not band.get("enabled", True) or band.get("filterType") in ("gain", "delay"):
            continue
        native = _GLOBAL_BAND_TYPES.get(str(band.get("filterType", "bell")))
        if native is None:
            raise _Unmodelled("global equalizer")
        gain_db = float(band.get("gainDb", 0.0))
        if native in _SHAPING_KINDS and abs(gain_db) < 1e-9:
            continue
        if not iir:
            raise _Unmodelled("global equalizer")
        frequency = float(band.get("frequencyHz", 1000.0))
        if not 0.0 < frequency < grid.rate / 2:
            raise _Unmodelled("global equalizer")
        coefficients = native_peq_coefficients(native, frequency, grid.rate, float(band.get("q", 1.0)), gain_db)
        shape = _biquad(grid, coefficients)
        response = shape if response is None else response * shape
    return response


def _read(paths: list[_RolePath], grid: _Grid, *, removed: str | None) -> float:
    """Carrier phase cosine the detector reads behind the summed paths."""
    total = np.zeros_like(grid.z1)
    for path in paths:
        response = path.base
        for stage in path.stages:
            if stage.name != removed:
                response = response * stage.response
        total = total + response
    impulse = np.roll(np.fft.irfft(total, grid.size), int(LEAD_SECONDS * grid.rate))
    magnitude = np.abs(impulse)
    if float(magnitude.max()) <= 1e-18:
        return 0.0
    return float(polarity.peak_phase(impulse, grid.rate, int(np.argmax(magnitude)))[0])


def _culprit(paths: list[_RolePath], grid: _Grid) -> str:
    """The stage whose removal restores the most readable phase."""
    names = list(dict.fromkeys(stage.name for path in paths for stage in path.stages))
    best, best_cos = "", abs(_read(paths, grid, removed=None))
    for name in names:
        candidate = abs(_read(paths, grid, removed=name))
        if candidate > best_cos:
            best, best_cos = name, candidate
    return best
