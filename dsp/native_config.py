# SPDX-License-Identifier: AGPL-3.0-only
"""Compile a processing plan into a native engine output layout.

Pure function over a validated processing plan. Preset payloads arrive
through ``preset_loader`` (for example ``DSPPresetStore.read``) and IR
files through ``resolve_ir`` so this module owns no files, no hardware,
and no global state. The manager renders the returned layout to engine
text; the native engine executes it.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from audio.output_topology import SUB_ROLES
from dsp.crossover import design_crossover

# Shared biquad budget per output, mirroring
# DSPManager.OUTPUT_FILTER_MAX_BIQUADS (kept local to avoid an import
# cycle; both must stay 32).
OUTPUT_BIQUAD_BUDGET = 32

_BAND_TYPES = {
    "bell": "bell", "pk": "bell", "notch": "notch",
    "low_shelf": "lowshelf", "high_shelf": "highshelf",
    "low_pass": "lowpass", "high_pass": "highpass",
}

_LEFT_SIDED = {"main_l", "sub_l"}


def role_side(role: str) -> str:
    """Return the stereo side a logical role belongs to, or "mono"."""
    if role in _LEFT_SIDED or role.startswith("left_"):
        return "left"
    if role in {"main_r", "sub_r"} or role.startswith("right_"):
        return "right"
    return "mono"


def bank_side(role: str, sub_mode: str = "stereo") -> str:
    """Return the input side a banked role is fed from, or "mono".

    Mirrors the plan's input routes: only a real ``sub_l``/``sub_r`` pair
    (``sub_mode == "stereo"``) feeds each sub from its own side.  Every other
    sub role — a lone mono sub or a dual-mono pair — sums both inputs at 0.5
    gain, so it is a mono bank whatever the role is called: a lone ``sub_l``
    is *not* a left-sided output.
    """
    if role in SUB_ROLES and sub_mode != "stereo":
        return "mono"
    return role_side(role)


def _finite(value: object, label: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _validate_band(band: object, index: int) -> dict:
    if not isinstance(band, dict):
        raise ValueError(f"PEQ band[{index}] must be an object")
    kind = _BAND_TYPES.get(str(band.get("filterType", "bell")))
    if kind is None and band.get("filterType") not in {"gain", "delay"}:
        raise ValueError(f"PEQ band[{index}].filterType is not supported")
    frequency = _finite(band.get("frequencyHz", 1000.0), f"PEQ band[{index}].frequencyHz")
    gain = _finite(band.get("gainDb", 0.0), f"PEQ band[{index}].gainDb")
    quality = _finite(band.get("q", 1.0), f"PEQ band[{index}].q")
    delay = _finite(band.get("delayMs", 0.0), f"PEQ band[{index}].delayMs")
    if not 20 <= frequency <= 20000:
        raise ValueError(f"PEQ band[{index}].frequencyHz must be between 20 and 20000")
    if not -24 <= gain <= 24 or not 0.1 <= quality <= 20:
        raise ValueError(f"PEQ band[{index}] gain or Q is outside the supported range")
    if not 0 <= delay <= 500:
        raise ValueError(f"PEQ band[{index}].delayMs must be between 0 and 500")
    return {"enabled": bool(band.get("enabled", True)),
            "filterType": band.get("filterType"), "native": kind,
            "frequencyHz": frequency, "gainDb": gain, "q": quality, "delayMs": delay}


def _project_bands(role: str, plugin: dict, sub_mode: str = "stereo") -> list[dict]:
    """Select the audible bands of one equalizer plugin for a mono role."""
    params = plugin.get("params") if isinstance(plugin.get("params"), dict) else {}
    mode = str(params.get("channelMode", "stereo-linked"))
    if mode not in {"stereo-linked", "dual"}:
        raise ValueError(f"Area bank {role} requires stereo-linked or dual PEQ")
    if str(params.get("eqMode", "IIR")).upper() != "IIR":
        raise ValueError(f"Area bank {role} requires IIR PEQ (no native biquad equivalent otherwise)")
    if mode == "dual":
        left = [_validate_band(band, index) for index, band in enumerate(params.get("leftBands", []))]
        right = [_validate_band(band, index) for index, band in enumerate(params.get("rightBands", []))]
        if len(left) > 20 or len(right) > 20:
            raise ValueError(f"Area bank {role} supports at most 20 bands per side")
        side = bank_side(role, sub_mode)
        if side == "mono" and left != right:
            raise ValueError(f"Area bank {role} sums both inputs and needs identical L/R bands")
        return {"left": left, "right": right, "mono": left}[side]
    bands = [_validate_band(band, index) for index, band in enumerate(params.get("bands", []))]
    if len(bands) > 20:
        raise ValueError(f"Area bank {role} supports at most 20 bands")
    return bands


def _oconv_for_bank(role: str, plugin: dict, resolve_ir: Callable[[str], dict],
                    sub_mode: str = "stereo") -> dict:
    params = plugin.get("params") if isinstance(plugin.get("params"), dict) else {}
    info = resolve_ir(params.get("kernel", ""))
    if not isinstance(info, dict) or not isinstance(info.get("path"), str) or not info["path"]:
        raise ValueError(f"Area bank {role} convolver needs a resolvable IR kernel")
    channels = info.get("channels")
    if type(channels) is not int or channels < 1:
        raise ValueError(f"Area bank {role} convolver needs a known IR channel count")
    side = bank_side(role, sub_mode)
    if channels == 1:
        channel = 0
    elif side == "mono":
        raise ValueError(f"Area bank {role} sums both inputs and needs a mono IR file")
    else:
        channel = 0 if side == "left" else 1
    return {
        "path": info["path"], "channel": channel,
        "wet_db": _finite(params.get("wet_db", 0.0), f"Area bank {role} wet_db"),
        "dry_db": _finite(params.get("dry_db", -100.0), f"Area bank {role} dry_db"),
        "input_gain_db": _finite(params.get("input_gain_db", 0.0), f"Area bank {role} input_gain_db"),
        "output_gain_db": _finite(params.get("output_gain_db", 0.0), f"Area bank {role} output_gain_db"),
    }


def layout_from_plan(plan: dict, *, resolve_ir: Callable[[str], dict]) -> list[dict]:
    """Map every plan output to a manager layout entry with sos/oconv data.

    The plan already carries resolved bank chains, so no preset loading is
    needed here; only IR files resolve late through ``resolve_ir``.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("outputs"), list):
        raise ValueError("Processing plan requires an outputs array")
    rate = plan.get("sample_rate_hz")
    if type(rate) is not int or rate <= 0:
        raise ValueError("Processing plan requires a positive integer sample rate")
    sub_mode = str(plan.get("sub_mode", ""))
    if sub_mode not in {"none", "mono", "stereo", "dual-mono", "unsupported"}:
        raise ValueError("Processing plan requires a known sub_mode")
    layout = []
    for output in plan["outputs"]:
        role = output.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError("Plan output requires a role name")
        sos: list[list[float]] = []
        for crossover in output.get("crossover", []):
            sos.extend(
                [b0, b1, b2, a1, a2]
                for b0, b1, b2, _, a1, a2 in design_crossover(dict(crossover), rate)
            )
        filters: list[dict] = []
        gain_db = float(output.get("gain_db", 0.0))
        delay_ms = float(output.get("delay_ms", 0.0))
        oconv = None
        equalizers = 0
        bank = output.get("bank") or {}
        if not bank.get("bypass"):
            for plugin in bank.get("chain", []):
                if not isinstance(plugin, dict) or not plugin.get("enabled", True):
                    continue
                plugin_type = plugin.get("type")
                if plugin_type == "equalizer":
                    equalizers += 1
                    for band in _project_bands(role, plugin, sub_mode):
                        if not band["enabled"]:
                            continue
                        if band["filterType"] == "gain":
                            gain_db += band["gainDb"]
                        elif band["filterType"] == "delay":
                            delay_ms += band["delayMs"]
                        else:
                            filters.append({"type": band["native"], "frequency_hz": band["frequencyHz"],
                                            "q": band["q"], "gain_db": band["gainDb"], "stages": 1})
                elif plugin_type == "convolver":
                    if oconv is not None:
                        raise ValueError(f"Area bank {role} supports a single convolver")
                    oconv = _oconv_for_bank(role, plugin, resolve_ir, sub_mode)
                else:
                    raise ValueError(f"Area bank {role} supports only PEQ and convolver presets")
        if equalizers > 1:
            raise ValueError(f"Area bank {role} supports a single equalizer")
        if sum(item["stages"] for item in filters) + len(sos) > OUTPUT_BIQUAD_BUDGET:
            raise ValueError(f"Area bank {role} exceeds {OUTPUT_BIQUAD_BUDGET} biquad stages")
        layout.append({"name": role, "routes": output.get("routes", []),
                       "gain_db": gain_db, "delay_ms": delay_ms,
                       "invert": bool(output.get("invert", False)),
                       "filters": filters, "sos": sos, "oconv": oconv})
    return layout
