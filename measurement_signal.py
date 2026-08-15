"""Pure sweep signal generation for measurement capture."""

from __future__ import annotations

import math
import wave
from pathlib import Path
from typing import Any

import numpy as np


def generate_log_sweep(
    sample_rate: int,
    duration_seconds: float,
    start_hz: float,
    end_hz: float,
    peak_scale: float = 0.8,
) -> np.ndarray:
    sample_count = max(2048, int(round(sample_rate * duration_seconds)))
    t = np.arange(sample_count, dtype=np.float64) / sample_rate
    log_ratio = math.log(end_hz / start_hz)
    phase = 2.0 * math.pi * start_hz * duration_seconds / log_ratio * (
        np.exp(t * log_ratio / duration_seconds) - 1.0
    )
    sweep = np.sin(phase).astype(np.float32)
    fade_len = min(sample_count // 8, max(64, int(round(sample_rate * 0.01))))
    if fade_len > 1:
        sweep[:fade_len] *= np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
        sweep[-fade_len:] *= np.linspace(1.0, 0.0, fade_len, dtype=np.float32)
    peak = float(np.max(np.abs(sweep))) or 1.0
    return (float(peak_scale) * sweep / peak).astype(np.float32)


def _fft_convolve(signal: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    fft_size = 1 << (signal.size + kernel.size - 2).bit_length()
    convolved = np.fft.irfft(
        np.fft.rfft(signal, n=fft_size) * np.fft.rfft(kernel, n=fft_size),
        n=fft_size,
    )
    return convolved[: signal.size + kernel.size - 1]


def build_inverse_sweep(
    sweep: np.ndarray,
    sample_rate: int,
    duration_seconds: float,
    start_hz: float,
    end_hz: float,
) -> np.ndarray:
    sample_count = max(1, int(sweep.size))
    t = np.arange(sample_count, dtype=np.float64) / sample_rate
    log_ratio = math.log(end_hz / start_hz)
    envelope = np.exp(-t * log_ratio / max(duration_seconds, 1e-9))
    inverse = sweep[::-1].astype(np.float64) * envelope
    reference_ir = _fft_convolve(sweep.astype(np.float64), inverse)
    peak = float(np.max(np.abs(reference_ir)))
    if peak <= 1e-12:
        raise RuntimeError("Unable to build inverse sweep kernel")
    inverse /= peak
    return inverse


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    clipped = np.clip(samples, -1.0, 1.0)
    int_samples = np.round(clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        channels = 1 if int_samples.ndim == 1 else int_samples.shape[1]
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(int_samples.tobytes())


def write_sweep_file(
    path: Path,
    *,
    sample_rate: int,
    sweep_seconds: float,
    lead_in_seconds: float,
    tail_seconds: float,
    channel: str,
    start_hz: float,
    end_hz: float,
    peak_scale: float = 0.8,
) -> dict[str, Any]:
    sweep = generate_log_sweep(sample_rate, sweep_seconds, start_hz, end_hz, peak_scale)
    inverse_sweep = build_inverse_sweep(sweep, sample_rate, sweep_seconds, start_hz, end_hz)
    lead_in = np.zeros(int(round(sample_rate * lead_in_seconds)), dtype=np.float32)
    tail = np.zeros(int(round(sample_rate * tail_seconds)), dtype=np.float32)
    mono_program = np.concatenate([lead_in, sweep, tail]).astype(np.float32)
    if channel == "right":
        playback = np.column_stack([np.zeros_like(mono_program), mono_program])
    elif channel == "stereo":
        playback = np.column_stack([mono_program, mono_program])
    else:
        playback = np.column_stack([mono_program, np.zeros_like(mono_program)])
    _write_wav(path, playback, sample_rate)
    playback64 = playback.astype(np.float64)
    peak = float(np.max(np.abs(playback64))) if playback64.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(playback64, dtype=np.float64)))) if playback64.size else 0.0
    per_channel_peak_dbfs = [
        round(20.0 * math.log10(max(float(np.max(np.abs(playback64[:, index]))), 1e-9)), 2)
        for index in range(playback64.shape[1])
    ] if playback64.ndim > 1 else []
    return {
        "analysis_sweep": sweep,
        "inverse_sweep": inverse_sweep,
        "sample_rate": int(sample_rate),
        "samples": int(mono_program.size),
        "channels": 2,
        "peak_linear": round(peak, 8),
        "peak_dbfs": round(20.0 * math.log10(max(peak, 1e-9)), 2),
        "rms_dbfs": round(20.0 * math.log10(max(rms, 1e-9)), 2),
        "per_channel_peak_dbfs": per_channel_peak_dbfs,
        "would_clip_before_write": bool(peak > 1.0),
    }
