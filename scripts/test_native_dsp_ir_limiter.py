#!/usr/bin/env python3
import array
import struct
import subprocess
import time
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DSP = ROOT / "native_dsp" / "build" / "fxroute-dsp-offline"


def run(tmp_path, config, samples, *, quantum=None, timeout=None):
    cfg = tmp_path / "dsp.conf"
    source = tmp_path / "in.f32"
    target = tmp_path / "out.f32"
    cfg.write_text(config)
    array.array("f", samples).tofile(source.open("wb"))
    command = [str(DSP), str(cfg), str(source), str(target)]
    if quantum is not None:
        command.append(str(quantum))
    result = subprocess.run(command, check=True, text=True, capture_output=True, timeout=timeout)
    output = array.array("f")
    output.fromfile(target.open("rb"), target.stat().st_size // 4)
    return output, result.stdout


def test_pcm_wav_ir_convolution_limiter_bypass_and_metering(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    ir = tmp_path / "echo.wav"
    with wave.open(str(ir), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(struct.pack("<hhh", 32767, 0, 16384))
    base = f"""rate 48000
inputs 1
outputs 1
matrix 0 0 1
stage_begin 0 convolver native convolver
param path "{ir}"
param wet_db 0
param dry_db -100
param input_gain_db 0
param output_gain_db 0
stage_end
stage_begin 1 headroom native headroom
param gain_db -6.020599913
stage_end
"""
    output, meters = run(tmp_path, base, [1, 0, 0, 0])
    assert abs(output[0] - 0.5) < 2e-4
    assert abs(output[2] - 0.25) < 2e-4
    assert "meter 0 peak" in meters and "rms" in meters

    bypassed, _ = run(tmp_path, base + "bypass 1\n", [1, 0, 0, 0])
    assert list(bypassed) == [1, 0, 0, 0]


def test_stereo_ir_channels_remain_independent(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    ir = tmp_path / "stereo.wav"
    with wave.open(str(ir), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(struct.pack("<hhhhhh", 32767, 0, 0, 16384, 8192, -8192))
    config = f"""rate 48000
inputs 1
outputs 2
matrix 0 0 1
matrix 1 0 1
stage_begin 0 convolver native convolver
param path "{ir}"
param wet_db 0
param dry_db -100
param input_gain_db 0
param output_gain_db 0
stage_end
"""
    output, _ = run(tmp_path, config, [1, 0, 0, 0], quantum=3)
    left = output[0::2]
    right = output[1::2]
    assert abs(left[0] - 32767 / 32768) < 1e-6
    assert abs(left[2] - 0.25) < 1e-6
    assert abs(right[1] - 0.5) < 1e-6
    assert abs(right[2] + 0.25) < 1e-6


def test_44100_hz_ir_is_resampled_for_48000_hz_engine(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    ir = tmp_path / "different-rate.wav"
    taps = array.array("h", [0]) * 442
    taps[0] = 16384
    taps[-1] = 16384
    with wave.open(str(ir), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(taps.tobytes())
    config = f"""rate 48000
inputs 1
outputs 1
matrix 0 0 1
stage_begin 0 convolver native convolver
param path "{ir}"
param wet_db 0
param dry_db -100
param input_gain_db 0
param output_gain_db 0
stage_end
"""
    samples = array.array("f", [0]) * 600
    samples[0] = 1
    output, _ = run(tmp_path, config, samples)
    significant = [index for index, value in enumerate(output) if abs(value) > 0.01]
    assert significant[-1] in range(478, 483)
    assert abs(sum(output) - 1.0) < 0.03


def test_32768_tap_ir_scales_for_realtime_use(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    ir = tmp_path / "long.wav"
    taps = array.array("h", [0]) * 32768
    taps[0] = 16384
    taps[-1] = 8192
    with wave.open(str(ir), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(taps.tobytes())
    config = f"""rate 48000
inputs 1
outputs 1
matrix 0 0 1
stage_begin 0 convolver native convolver
param path "{ir}"
param wet_db 0
param dry_db -100
param input_gain_db 0
param output_gain_db 0
stage_end
"""
    samples = array.array("f", [0]) * 32768
    samples[0] = 1
    started = time.monotonic()
    output, _ = run(tmp_path, config, samples, quantum=173, timeout=1)
    assert time.monotonic() - started < 1
    assert abs(output[0] - 0.5) < 1e-6
    assert abs(output[-1] - 0.25) < 1e-6


def test_source_has_no_rt_allocation_or_io_calls():
    engine = (ROOT / "native_dsp" / "pipewire_engine.c").read_text()
    start = engine.index("static void on_process")
    bodies = [engine[start:engine.index("\n}", start)]]
    dsp = (ROOT / "native_dsp" / "dsp.c").read_text()
    start = dsp.index("void fxdsp_process")
    bodies.append(dsp[start:dsp.index("void fxdsp_meter", start)])
    for forbidden in ("malloc(", "calloc(", "realloc(", "free(", "fopen(", "read(", "write(", "socket("):
        assert all(forbidden not in body for body in bodies)
