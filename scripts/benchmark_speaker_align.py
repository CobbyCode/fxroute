#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""CPU-only Speaker Align benchmark; never opens an audio device or starts jobs.

Use --source-root to compare an older checkout with identical fixtures.
"""

import argparse
import hashlib
import json
import logging
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    sys.path.insert(0, str(args.source_root.resolve()))
    from dsp.crossover import design_crossover
    from measurement.signal import write_sweep_file
    from measurement.speaker_verification import side_confirmation
    from measurement.store import MeasurementStore

    logging.disable(logging.CRITICAL)
    rate = 48000
    models = {
        "right_low": {"crossover": [
            {"kind": "lowpass", "frequency_hz": 3000., "family": "linkwitz-riley", "slope_db_oct": 24},
            {"kind": "highpass", "frequency_hz": 80., "family": "linkwitz-riley", "slope_db_oct": 24}]},
        "right_high": {"crossover": [
            {"kind": "highpass", "frequency_hz": 3000., "family": "linkwitz-riley", "slope_db_oct": 24},
            {"kind": "highpass", "frequency_hz": 80., "family": "linkwitz-riley", "slope_db_oct": 24}]},
    }
    report = {"source_root": str(args.source_root.resolve()), "rate": rate, "repeats": args.repeats}

    def measure(label, operation):
        samples = []
        for _ in range(args.repeats):
            start = time.perf_counter()
            value = operation()
            samples.append((time.perf_counter() - start) * 1000)
        report[label] = {"first_ms": round(samples[0], 3), "samples_ms": [round(t, 3) for t in samples],
                         "warm_median_ms": round(statistics.median(samples[1:] or samples), 3)}
        return value

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def response(model, size):
        frequencies = np.fft.rfftfreq(size, 1. / rate)
        z = np.exp(-2j * np.pi * frequencies / rate)
        result = np.ones_like(z)
        for spec in model["crossover"]:
            for b0, b1, b2, _, a1, a2 in design_crossover(spec, rate):
                result *= (b0 + b1 * z + b2 * z * z) / (1 + a1 * z + a2 * z * z)
        return result

    with tempfile.TemporaryDirectory(prefix="speaker-cpu-bench-") as home:
        os.environ["XDG_CONFIG_HOME"] = str(Path(home) / "config")
        os.environ["XDG_STATE_HOME"] = str(Path(home) / "state")
        store = MeasurementStore(home=Path(home))
        sweep = measure("signal_preparation", lambda: write_sweep_file(
            store.playbacks_dir / "dry.wav", sample_rate=rate, sweep_seconds=11.,
            lead_in_seconds=0.5, tail_seconds=1.25, channel="right", start_hz=10., end_hz=22000.))
        report["signal_sha256"] = hashlib.sha256(
            sweep["analysis_sweep"].tobytes() + sweep["inverse_sweep"].tobytes()).hexdigest()
        report["wav_sha256"] = hashlib.sha256((store.playbacks_dir / "dry.wav").read_bytes()).hexdigest()

        program = np.concatenate([np.zeros(60000), sweep["analysis_sweep"], np.zeros(96000)])
        size = 1 << (program.size - 1).bit_length()
        frequencies = np.fft.rfftfreq(size, 1. / rate)
        combined = sum(response(model, size) * np.exp(-2j * np.pi * frequencies * delay)
                       for model, delay in zip(models.values(), (0.0075, 0.0)))
        rendered = np.fft.irfft(np.fft.rfft(program, n=size) * combined, n=size)[:program.size]
        mic = np.zeros_like(rendered)
        mic[76:] = rendered[:-76] * 0.03
        recording = np.zeros((program.size, 8))
        recording[:, 0] = mic
        recording[:, 7] = rendered * 0.1
        path = store.captures_dir / "dry.wav"
        store._write_wav(path, recording, rate)
        calibration = (np.array([20., 200., 3000., 22000.]), np.array([-1., 0.4, -0.5, 1.2]))

        def analyze():
            store._last_successful_lag = None
            return store._analyzer._analyze_sweep_capture(
                path, expected_sample_rate=rate, channel="right",
                reference_sweep=sweep["analysis_sweep"], inverse_sweep=sweep["inverse_sweep"],
                calibration_curve=calibration, reference_channel_index=7, analysis_channel_index=0,
                reference_channel_label="input_8_electrical_reference", band_limited_reference=True)

        analysis = measure("capture_analysis", analyze)
        report["analysis_sha256"] = digest(analysis)

        # Match the full-resolution IR size of the observed real planning take.
        ir = np.zeros(1130425)
        kernel_size = 32768
        grid = np.fft.rfftfreq(kernel_size, 1. / rate)
        spectrum = sum(response(model, kernel_size) * np.exp(-2j * np.pi * grid * delay)
                       for model, delay in zip(models.values(), (0.0075, 0.0)))
        kernel = np.fft.irfft(spectrum, n=kernel_size)
        ir[528000:528000 + kernel.size] = kernel
        document = measure("shared_planning", lambda: side_confirmation(
            impulse_response=ir, processing=models, roles=list(models), sample_rate_hz=rate,
            start_revision=1, processing_fingerprint="cpu-benchmark"))
        report["planning_sha256"] = digest(document)
        report["arrival_ms"] = document["arrival_ms"]
        report["way_levels_db"] = document["way_levels_db"]

    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
