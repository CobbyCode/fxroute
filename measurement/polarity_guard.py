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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from dsp.native_model import native_peq_coefficients, read_ir, resample_ir
from measurement import polarity

logger = logging.getLogger(__name__)

OK = "ok"
ROTATES = "rotates"
UNKNOWN = "unknown"
REASON_FILTER_IN_BAND = "dsp_filter_in_band"
REASON_UNKNOWN = "dsp_unknown"
REASONS = {ROTATES: REASON_FILTER_IN_BAND, UNKNOWN: REASON_UNKNOWN}
# The modelled carrier phase must stay within about 37 degrees of 0 or 180.
GUARD_MIN_COS = 0.8
# The detector reads the sample nearest the envelope peak, so the sub-sample position at which a real
# take arrives changes the phase it reads, by more the steeper the path's phase slope. One reading of
# the model is one arbitrary position. The path is therefore read at these offsets (in samples) as well,
# and every reading must keep the sign of the first one: a position that flips it could turn a correct
# wiring into "reversed". The offsets span one sample, the period of the sample grid.
ARRIVAL_OFFSETS = tuple(np.linspace(-0.5, 0.5, 17))
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


@dataclass
class _Context:
    """The frequency grid of one model run and the choices that make its variants."""

    rate: int
    size: int
    z1: np.ndarray
    z2: np.ndarray
    omega: np.ndarray
    surround: bool
    kernel_path: Callable[[str], str] | None
    irs: dict[str, tuple[np.ndarray, int]] = field(default_factory=dict)
    # A Global stage of a centre or LFE input runs on a pair whose channel is not named here.
    side: str | None = None
    # A two-channel Global IR does not say which channel a speaker input uses.
    ir_channel: int = 0
    stereo_ir: bool = False


def unknown_result(stage: str) -> dict:
    return {"state": UNKNOWN, "dsp_cos": None, "stage": stage}


def assess_unit(config: Mapping[str, Any], unit: Mapping[str, Any], *, sample_rate_hz: int,
                kernel_path: Callable[[str], str] | None = None) -> dict:
    """Judge the rendered path of one polarity unit.

    ``config`` is the engine configuration of the rendered plan (``outputs`` and
    the Global ``chain``); ``kernel_path`` resolves a Global convolver's kernel
    name to its file. Returns ``state`` (ok / rotates / unknown), ``dsp_cos``
    (carrier phase cosine the detector would read behind an ideal speaker, signed)
    and ``stage`` (the stage that rotates or cannot be modelled, else "").
    """
    try:
        return _assess(config, unit, sample_rate_hz, kernel_path)
    except _Unmodelled as exc:
        return unknown_result(exc.stage)
    except (ArithmeticError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Polarity guard could not model the DSP path: %s", exc)
        return unknown_result("DSP path")


def _assess(config: Mapping[str, Any], unit: Mapping[str, Any], rate: int,
            kernel_path: Callable[[str], str] | None) -> dict:
    if type(rate) is not int or rate <= 0:
        raise ValueError("Sample rate must be a positive integer")
    outputs = {output["name"]: output for output in config["outputs"]}
    roles = list(unit["roles"])
    if not roles or any(role not in outputs for role in roles):
        raise _Unmodelled("unit roles")
    size = 1 << math.ceil(math.log2(MODEL_SECONDS * rate))
    omega = 2.0 * np.pi * np.arange(size // 2 + 1) / size
    z1 = np.exp(-1j * omega)
    context = _Context(rate=rate, size=size, z1=z1, z2=z1 * z1, omega=omega,
                       surround=any("input_positions" in output for output in outputs.values()),
                       kernel_path=kernel_path)
    chain = [plugin for plugin in config.get("chain", []) if plugin.get("enabled", True)]
    ambiguous = any(_driven_input(outputs[role]) in (2, 3) for role in roles)
    least_readable: tuple[_Reading, list[_RolePath]] | None = None
    for side in (("left", "right") if ambiguous else (None,)):
        for ir_channel in (0, 1):
            context.side, context.ir_channel, context.stereo_ir = side, ir_channel, False
            paths = [_role_path(outputs[role], role, chain, context) for role in roles]
            reading = _read(paths, context, removed=None)
            if least_readable is None or reading.score < least_readable[0].score:
                least_readable = (reading, paths)
            if not context.stereo_ir:
                break
    assert least_readable is not None
    reading, paths = least_readable
    if reading.readable:
        return {"state": OK, "dsp_cos": round(reading.cos, 3), "stage": ""}
    return {"state": ROTATES, "dsp_cos": round(reading.cos, 3), "stage": _culprit(paths, context)}


def _driven_input(output: Mapping[str, Any]) -> int:
    """Engine input the sweep excites for this output: its first route (the speaker's own position)."""
    routes = output.get("routes")
    if not isinstance(routes, list) or not routes:
        raise _Unmodelled("input routes")
    return int(routes[0]["input"])


def _biquad(context: _Context, coefficients: tuple[float, ...]) -> np.ndarray:
    b0, b1, b2, a1, a2 = coefficients
    return (b0 + b1 * context.z1 + b2 * context.z2) / (1.0 + a1 * context.z1 + a2 * context.z2)


def _role_path(output: Mapping[str, Any], role: str, chain: list[dict], context: _Context) -> _RolePath:
    if output.get("post_crossover_routes") or output.get("crossover_groups") or output.get("crossover_sos"):
        raise _Unmodelled(f"{role} bass bus")
    source = _driven_input(output)
    gain = float(output["routes"][0].get("gain", 1.0))
    level = 10.0 ** (float(output.get("gain_db", 0.0)) / 20.0)
    # The engine rounds the output delay to whole samples.
    delay_samples = math.floor(float(output.get("delay_ms", 0.0)) * context.rate / 1000.0 + 0.5)
    base = (gain * level * (-1.0 if output.get("invert") else 1.0)) * np.exp(-1j * context.omega * delay_samples)
    stages = [_Stage(name, response) for name, response in _global_stages(chain, source, context)]
    stages.extend(_output_stages(output, role, context))
    return _RolePath(base=base, stages=tuple(stages))


def _output_stages(output: Mapping[str, Any], role: str, context: _Context) -> list[_Stage]:
    """Crossover, area-bank PEQ and convolver of one output."""
    stages = []
    sections = output.get("sos") or []
    if sections:
        response = np.ones_like(context.z1)
        for section in sections:
            response = response * _biquad(context, tuple(float(value) for value in section))
        stages.append(_Stage(f"{role} crossover", response))
    bands = output.get("filters") or []
    if bands:
        response = np.ones_like(context.z1)
        for band in bands:
            frequency = float(band["frequency_hz"])
            if not 0.0 < frequency < context.rate / 2:
                raise _Unmodelled(f"{role} PEQ")
            coefficients = native_peq_coefficients(
                str(band["type"]), frequency, context.rate, float(band["q"]), float(band["gain_db"]))
            response = response * _biquad(context, coefficients) ** int(band.get("stages", 1))
        stages.append(_Stage(f"{role} PEQ", response))
    oconv = output.get("oconv")
    if oconv:
        name = f"{role} convolver {Path(str(oconv.get('path', ''))).name}".rstrip()
        stages.append(_Stage(name, _convolver_response(oconv, str(oconv.get("path", "")), name,
                                                       int(oconv.get("channel", 0)), context)))
    return stages


def _read_ir_cached(path: str, context: _Context) -> tuple[np.ndarray, int]:
    if path not in context.irs:
        context.irs[path] = read_ir(path)
    return context.irs[path]


def _convolver_response(params: Mapping[str, Any], path: str, name: str, channel: int,
                        context: _Context) -> np.ndarray:
    """``input gain * output gain * (dry + wet * IR)`` of a convolver, the IR resampled to the stream rate.

    Both convolvers (area bank and Global) run the same native stage; an IR that
    cannot be read or has no such channel makes the path unknown.
    """
    try:
        frames, rate_in = _read_ir_cached(path, context)
        if channel >= frames.shape[1]:
            raise ValueError("IR channel is out of range")
        wet, dry, gain_in, gain_out = (
            10.0 ** (float(params.get(key, default)) / 20.0) for key, default in (
                ("wet_db", 0.0), ("dry_db", -100.0), ("input_gain_db", 0.0), ("output_gain_db", 0.0)))
    except (OSError, ValueError, TypeError) as exc:
        raise _Unmodelled(name) from exc
    taps = resample_ir(frames[:, channel], rate_in, context.rate)[:context.size]
    return gain_in * gain_out * (dry + wet * np.fft.rfft(taps, context.size))


def _global_stages(chain: list[dict], source: int, context: _Context) -> list[tuple[str, np.ndarray]]:
    """Responses of the enabled Global plugins that act on one engine input."""
    group = _POSITION_GROUP.get(source) if context.surround else None
    side = context.side or _POSITION_SIDE.get(source) or "left"
    stages = []
    for plugin in chain:
        # A Surround Global plugin runs only on the effect pairs of its speaker group.
        if context.surround and plugin.get("group") and plugin["group"] != group:
            continue
        kind = plugin.get("type")
        if kind in NEUTRAL_GLOBAL_STAGES:
            continue
        if kind == "equalizer":
            response = _global_equalizer(plugin, side, context)
            if response is not None:
                stages.append(("global equalizer", response))
        elif kind == "convolver":
            stages.append(_global_convolver(plugin, context))
        else:
            raise _Unmodelled(f"global {str(kind or 'stage').replace('_', ' ')}")
    return stages


def _global_convolver(plugin: Mapping[str, Any], context: _Context) -> tuple[str, np.ndarray]:
    params = plugin.get("params") if isinstance(plugin.get("params"), Mapping) else {}
    kernel = str(params.get("kernel", ""))
    name = f"global convolver {Path(kernel).name}".rstrip()
    if context.kernel_path is None:
        raise _Unmodelled(name)
    try:
        path = str(context.kernel_path(kernel))
    except (OSError, ValueError, TypeError) as exc:
        raise _Unmodelled(name) from exc
    try:
        channels = _read_ir_cached(path, context)[0].shape[1]
    except (OSError, ValueError) as exc:
        raise _Unmodelled(name) from exc
    # A one-channel IR serves both sides; a stereo IR is modelled per channel, the worse one counts.
    context.stereo_ir = context.stereo_ir or channels > 1
    channel = min(context.ir_channel, channels - 1)
    return name, _convolver_response(params, path, name, channel, context)


def _global_equalizer(plugin: Mapping[str, Any], channel: str, context: _Context) -> np.ndarray | None:
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
        if not 0.0 < frequency < context.rate / 2:
            raise _Unmodelled("global equalizer")
        coefficients = native_peq_coefficients(native, frequency, context.rate, float(band.get("q", 1.0)), gain_db)
        shape = _biquad(context, coefficients)
        response = shape if response is None else response * shape
    return response


@dataclass(frozen=True)
class _Reading:
    cos: float
    # The reading that comes closest to the opposite sign over the arrival offsets, signed like ``cos``.
    same_sign: float

    @property
    def readable(self) -> bool:
        return abs(self.cos) >= GUARD_MIN_COS and self.same_sign > 0.0

    @property
    def score(self) -> float:
        """How far from unreadable: negative once either requirement fails."""
        return min(abs(self.cos) - GUARD_MIN_COS, self.same_sign)


def _carrier_cos(total: np.ndarray, context: _Context, offset: float) -> float:
    """Carrier phase cosine the detector reads when the take arrives ``offset`` samples off the grid."""
    spectrum = total if offset == 0.0 else total * np.exp(-1j * context.omega * offset)
    impulse = np.roll(np.fft.irfft(spectrum, context.size), int(LEAD_SECONDS * context.rate))
    magnitude = np.abs(impulse)
    if float(magnitude.max()) <= 1e-18:
        return 0.0
    return float(polarity.peak_phase(impulse, context.rate, int(np.argmax(magnitude)))[0])


def _read(paths: list[_RolePath], context: _Context, *, removed: str | None) -> _Reading:
    """What the detector reads behind the summed paths, at the grid and over the arrival offsets."""
    total = np.zeros_like(context.z1)
    for path in paths:
        response = path.base
        for stage in path.stages:
            if stage.name != removed:
                response = response * stage.response
        total = total + response
    cos = _carrier_cos(total, context, 0.0)
    sign = -1.0 if cos < 0.0 else 1.0
    same_sign = sign * cos
    # A path below the threshold is withheld anyway; the offsets only matter for a readable one.
    if abs(cos) >= GUARD_MIN_COS:
        for offset in ARRIVAL_OFFSETS:
            same_sign = min(same_sign, sign * _carrier_cos(total, context, float(offset)))
    return _Reading(cos=cos, same_sign=same_sign)


def _culprit(paths: list[_RolePath], context: _Context) -> str:
    """The stage whose removal restores the most readable phase."""
    names = list(dict.fromkeys(stage.name for path in paths for stage in path.stages))
    best, best_score = "", _read(paths, context, removed=None).score
    for name in names:
        score = _read(paths, context, removed=name).score
        if score > best_score:
            best, best_score = name, score
    return best
