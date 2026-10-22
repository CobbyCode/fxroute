# SPDX-License-Identifier: AGPL-3.0-only
"""Python mirrors of native engine stages: PEQ biquad design and IR reading.

Pure functions shared by the AutoSub peak predictor and the Polarity Check
guard, so both model a bank the way the engine renders it.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from dsp.manager import ensure_kernel_supported_ir, parse_wav_frames


def native_peq_coefficients(
    filter_type: str, frequency_hz: float, sample_rate: int,
    q: float, gain_db: float,
) -> tuple[float, float, float, float, float]:
    """Mirror the native engine's PEQ biquad design (RBJ cookbook, S=1 shelves).

    Returns normalized ``(b0, b1, b2, a1, a2)`` exactly as native_dsp/dsp.c
    ``design()`` computes them, so the predictor and the engine agree on
    every bank PEQ response.
    """
    frequency = float(frequency_hz)
    rate = float(int(sample_rate))
    quality = float(q)
    gain = float(gain_db)
    if not quality > 0.0:
        raise ValueError("PEQ q must be positive")
    a = 10.0 ** (gain / 40.0)
    w = 2.0 * math.pi * frequency / rate
    cs, sn = math.cos(w), math.sin(w)
    alpha = sn / (2.0 * quality)
    beta = 2.0 * math.sqrt(a) * alpha
    if filter_type == "bell":
        b0, b1, b2, a0, a1, a2 = (1 + alpha * a, -2 * cs, 1 - alpha * a,
                                  1 + alpha / a, -2 * cs, 1 - alpha / a)
    elif filter_type == "notch":
        b0, b1, b2, a0, a1, a2 = (1, -2 * cs, 1, 1 + alpha, -2 * cs, 1 - alpha)
    elif filter_type == "lowpass":
        b0, b1, b2, a0, a1, a2 = ((1 - cs) / 2, 1 - cs, (1 - cs) / 2,
                                  1 + alpha, -2 * cs, 1 - alpha)
    elif filter_type == "highpass":
        b0, b1, b2, a0, a1, a2 = ((1 + cs) / 2, -(1 + cs), (1 + cs) / 2,
                                  1 + alpha, -2 * cs, 1 - alpha)
    elif filter_type == "lowshelf":
        b0, b1, b2, a0, a1, a2 = (a * ((a + 1) - (a - 1) * cs + beta),
                                  2 * a * ((a - 1) - (a + 1) * cs),
                                  a * ((a + 1) - (a - 1) * cs - beta),
                                  (a + 1) + (a - 1) * cs + beta,
                                  -2 * ((a - 1) + (a + 1) * cs),
                                  (a + 1) + (a - 1) * cs - beta)
    elif filter_type == "highshelf":
        b0, b1, b2, a0, a1, a2 = (a * ((a + 1) + (a - 1) * cs + beta),
                                  -2 * a * ((a - 1) + (a + 1) * cs),
                                  a * ((a + 1) + (a - 1) * cs - beta),
                                  (a + 1) - (a - 1) * cs + beta,
                                  2 * ((a - 1) - (a + 1) * cs),
                                  (a + 1) - (a - 1) * cs - beta)
    else:
        raise ValueError(f"Unsupported PEQ filter type: {filter_type}")
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def read_mono_ir(path: str, channel: int) -> np.ndarray:
    """Read one IR channel as float64 samples, failing closed on bad files.

    Uses the same WAV parsing and kernel-support gate as the manager, so the
    predictor only ever models IR files the native convolver would load.
    """
    if type(channel) is not int or channel < 0:
        raise ValueError("IR channel must be a non-negative integer")
    name = str(path) or ""
    try:
        params = parse_wav_frames(Path(name))
    except (OSError, ValueError) as exc:
        raise ValueError(f"IR file is not readable: {name or path}") from exc
    try:
        ensure_kernel_supported_ir(params, Path(name).name)
    except ValueError as exc:
        raise ValueError(f"IR file has an unsupported encoding: {name}") from exc
    if channel >= params["channels"]:
        raise ValueError(
            f"IR channel {channel} exceeds the {params['channels']} channels of {name}")
    data = params["data"]
    if params["format"] == 3:
        samples = np.frombuffer(data, dtype="<f4").astype(np.float64)
    elif params["bits"] == 16:
        samples = np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0
    elif params["bits"] == 32:
        samples = np.frombuffer(data, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raw = np.frombuffer(data, dtype=np.uint8).astype(np.int32).reshape(-1, 3)
        signed = (raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)).astype(np.float64)
        signed -= 16777216.0 * (signed >= 8388608.0)
        samples = signed / 8388608.0
    frames = params["frames"]
    if samples.size != frames * params["channels"]:
        raise ValueError(f"IR file has truncated frames: {name}")
    return np.ascontiguousarray(samples.reshape(frames, params["channels"])[:, channel])
