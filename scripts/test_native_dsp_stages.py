#!/usr/bin/env python3
import array
import math
import struct
import subprocess
import sys
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DSP = ROOT / "native_dsp/build/fxroute-dsp-offline"
sys.path.insert(0, str(ROOT))

from dsp_manager import DSPManager


def run(tmp_path, stages, samples, *, outputs=2, quantum=509, check=True):
    config = tmp_path / "dsp.conf"
    source = tmp_path / "input.f32"
    target = tmp_path / "output.f32"
    routes = "\n".join(f"matrix {channel} {channel % 2} 1" for channel in range(outputs))
    post = "\n".join(f"output {channel} 0 0 normal" for channel in range(outputs))
    config.write_text(
        f"rate 48000\ninputs 2\noutputs {outputs}\n{routes}\n{stages}\n{post}\nbypass 0\n"
    )
    array.array("f", samples).tofile(source.open("wb"))
    result = subprocess.run(
        [str(DSP), str(config), str(source), str(target), str(quantum)],
        text=True, capture_output=True, check=check,
    )
    values = array.array("f")
    if target.exists():
        values.fromfile(target.open("rb"), target.stat().st_size // 4)
    return values, result


def stage(ordinal, identity, kind, params=""):
    body = f"stage_begin {ordinal} {identity} native {kind}\n"
    if params:
        body += params.rstrip() + "\n"
    return body + "stage_end"


def stereo_constant(value, frames):
    return [value for _ in range(frames) for _ in range(2)]


def stereo_sine(amplitude, frames, frequency=997.0, rate=48000):
    return [amplitude * math.sin(2 * math.pi * frequency * index / rate)
            for index in range(frames) for _ in range(2)]


def test_stage_order_is_noncommutative_for_headroom_and_autogain(tmp_path):
    subprocess.run([str(ROOT / "native_dsp/build.sh")], check=True)
    gain = stage(0, "gain", "headroom", "param gain_db -30")
    auto = stage(1, "auto", "autogain", "\n".join((
        "param target_db -12", 'param reference "Momentary"',
        "param silence_threshold_db -50", "param maximum_history_seconds 6")))
    samples = stereo_sine(.2, 48000)
    gain_then_auto, _ = run(tmp_path, gain + "\n" + auto, samples)
    auto = auto.replace("stage_begin 1", "stage_begin 0")
    gain = gain.replace("stage_begin 0", "stage_begin 1")
    auto_then_gain, _ = run(tmp_path, auto + "\n" + gain, samples)
    assert max(abs(a - b) for a, b in zip(gain_then_auto[-4096:], auto_then_gain[-4096:])) > .01


def test_repeated_native_stages_are_not_collapsed(tmp_path):
    subprocess.run([str(ROOT / "native_dsp/build.sh")], check=True)
    once = stage(0, "first", "headroom", "param gain_db -6.020599913")
    twice = once + "\n" + stage(1, "second", "headroom", "param gain_db -6.020599913")
    samples = stereo_constant(.8, 8)
    one, _ = run(tmp_path, once, samples)
    two, _ = run(tmp_path, twice, samples)
    assert abs(one[0] - .4) < 1e-5
    assert abs(two[0] - .2) < 1e-5


def test_stage_grammar_is_strict(tmp_path):
    subprocess.run([str(ROOT / "native_dsp/build.sh")], check=True)
    invalid = (
        "stage_begin 1 skipped native headroom\nparam gain_db -3\nstage_end",
        "stage_begin 0 open native headroom\nparam gain_db -3",
        "stage_begin 0 bad native headroom\ncontrol gain_db -3\nstage_end",
        "stage_begin 0 bad native delay\nparam left_ms 1\nparam unknown 2\nstage_end",
        "headroom_db -3",
    )
    for index, text in enumerate(invalid):
        case = tmp_path / str(index)
        case.mkdir()
        _, result = run(case, text, [0., 0.], check=False)
        assert result.returncode == 1, text


def test_native_convolver_delay_and_crystalizer_integration(tmp_path):
    subprocess.run([str(ROOT / "native_dsp/build.sh")], check=True)
    ir = tmp_path / "impulse.wav"
    with wave.open(str(ir), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(48000)
        wav.writeframes(struct.pack("<hh", 32767, 16384))
    stages = "\n".join((
        stage(0, "conv", "convolver", "\n".join((
            f'param path "{ir}"', "param wet_db 0", "param dry_db -100",
            "param input_gain_db 0", "param output_gain_db 0"))),
        stage(1, "delay", "delay", "param left_ms 0\nparam right_ms 0.0208333333"),
        stage(2, "crystal", "crystalizer", "param intensity_band2_db -2"),
    ))
    impulse = [0.] * 8192
    impulse[0] = impulse[1] = 1.
    output, _ = run(tmp_path, stages, impulse, quantum=1001)
    assert len(output) == len(impulse)
    assert all(math.isfinite(value) for value in output)
    assert all(abs(value) < 1e-7 for value in output[:2048])
    left_peak = max(range(0, len(output), 2), key=lambda index: abs(output[index]))
    right_peak = max(range(1, len(output), 2), key=lambda index: abs(output[index]))
    assert abs(output[left_peak]) > 1.
    assert abs(output[right_peak]) > 1.
    assert right_peak == left_peak + 3


def test_lv2_requires_an_even_input_count_before_instantiation(tmp_path):
    subprocess.run([str(ROOT / "native_dsp/build.sh")], check=True)
    text = "stage_begin 0 plugin lv2 urn:missing:test\nstage_end"
    config = tmp_path / "odd.conf"
    source = tmp_path / "odd-in.f32"
    target = tmp_path / "odd-out.f32"
    config.write_text(
        f"rate 48000\ninputs 3\noutputs 3\n"
        "matrix 0 0 1\nmatrix 1 1 1\nmatrix 2 2 1\n"
        f"{text}\n"
    )
    array.array("f", [0.0, 0.0, 0.0]).tofile(source.open("wb"))
    result = subprocess.run(
        [str(DSP), str(config), str(source), str(target)],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 1
    assert "even" in result.stderr.lower() or "stereo" in result.stderr.lower()


def test_parser_loads_manager_generated_maximum_dual_peq(tmp_path):
    subprocess.run([str(ROOT / "native_dsp/build.sh")], check=True)
    manager = DSPManager(home=tmp_path / "home")
    bands = [
        {"filterType": "bell", "frequencyHz": 100 + index * 100,
         "gainDb": 0, "q": 1}
        for index in range(20)
    ]
    manager.create_peq_preset("Maximum", {
        "params": {"channelMode": "dual", "leftBands": bands, "rightBands": bands},
    })
    manager.load_preset("Maximum")
    config = tmp_path / "maximum.conf"
    config.write_text(manager.compile_engine_text([
        {"name": "FL", "source": 0}, {"name": "FR", "source": 1},
    ]))
    source = tmp_path / "maximum.f32"
    target = tmp_path / "maximum-output.f32"
    array.array("f", [0., 0.]).tofile(source.open("wb"))

    result = subprocess.run(
        [str(DSP), str(config), str(source), str(target), "1"],
        text=True, capture_output=True,
    )

    equalizer_stage = config.read_text().split("stage_end", 1)[0]
    assert equalizer_stage.count("control ") == 268
    assert result.returncode == 0, result.stderr


from native_test_runner import run_pytest_style_module

if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
