#!/usr/bin/env python3
import array
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DSP = ROOT / "native_dsp" / "build" / "fxroute-dsp-offline"


def build():
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)


def process(tmp_path, config, samples, channels):
    cfg = tmp_path / "dsp.conf"
    source = tmp_path / "in.f32"
    target = tmp_path / "out.f32"
    cfg.write_text(config)
    array.array("f", samples).tofile(source.open("wb"))
    subprocess.run([str(DSP), str(cfg), str(source), str(target)], check=True)
    result = array.array("f")
    result.fromfile(target.open("rb"), target.stat().st_size // 4)
    return [result[i::channels] for i in range(channels)]


def test_sparse_matrix_preserves_stereo_and_mono_sub_semantics(tmp_path):
    build()
    config = """rate 48000
inputs 2
outputs 4
matrix 0 0 1
matrix 1 1 1
matrix 2 0 0.5
matrix 2 1 0.5
matrix 3 0 0.5
matrix 3 1 0.5
"""
    out = process(tmp_path, config, [1, 0, 0, 1, 0, 0], 4)
    assert list(out[0]) == [1, 0, 0]
    assert list(out[1]) == [0, 1, 0]
    assert list(out[2]) == [0.5, 0.5, 0]
    assert list(out[3]) == [0.5, 0.5, 0]


def test_32_channels_delay_gain_and_polarity(tmp_path):
    build()
    routes = "\n".join(f"matrix {i} {i} 1" for i in range(32))
    config = f"""rate 1000
inputs 32
outputs 32
{routes}
output 31 -6.020599913 2 invert
"""
    frame = [0.0] * 32
    frame[31] = 1.0
    out = process(tmp_path, config, frame * 4, 32)
    assert list(out[31][:2]) == [0, 0]
    assert abs(out[31][2] + 0.5) < 1e-5


def test_global_delay_adds_to_output_alignment(tmp_path):
    build()
    config = """rate 1000
inputs 1
outputs 1
matrix 0 0 1
stage_begin 0 delay native delay
param left_ms 3
param right_ms 3
stage_end
output 0 0 2 normal
"""
    out = process(tmp_path, config, [1.0] + [0.0] * 6, 1)
    assert list(out[0][:5]) == [0.0] * 5
    assert out[0][5] == 1.0


def test_global_delay_rejects_more_than_one_second(tmp_path):
    build()
    config = """rate 1000
inputs 1
outputs 1
matrix 0 0 1
stage_begin 0 delay native delay
param left_ms 1001
param right_ms 0
stage_end
"""
    cfg = tmp_path / "dsp.conf"
    source = tmp_path / "in.f32"
    target = tmp_path / "out.f32"
    cfg.write_text(config)
    array.array("f", [1.0]).tofile(source.open("wb"))
    result = subprocess.run([str(DSP), str(cfg), str(source), str(target)])
    assert result.returncode != 0


def test_accumulated_peq_delay_can_exceed_one_second(tmp_path):
    build()
    config = """rate 1000
inputs 1
outputs 1
matrix 0 0 1
stage_begin 0 equalizer#0-delay native delay
param left_ms 1500
param right_ms 1500
stage_end
"""
    out = process(tmp_path, config, [1.0] + [0.0] * 1500, 1)
    assert out[0][1499] == 0.0
    assert out[0][1500] == 1.0


def test_direct_bypasses_effects_but_keeps_routing_crossover_and_alignment(tmp_path):
    build()
    config = """rate 1000
inputs 2
outputs 4
matrix 0 0 1
matrix 1 1 1
matrix 2 0 0.5
matrix 2 1 0.5
matrix 3 0 0.5
matrix 3 1 0.5
peq 2 lowpass 100 0.70710678 0
peq 3 lowpass 100 0.70710678 0
stage_begin 0 effect native headroom
param gain_db -6.020599913
stage_end
output 2 -6.020599913 1 invert
bypass 1
"""
    out = process(tmp_path, config, [1.0, 1.0] + [0.0, 0.0] * 5, 4)
    assert out[0][0] == 1.0
    assert out[1][0] == 1.0
    assert out[2][0] == 0.0
    assert out[2][1] < 0.0
    assert abs(out[2][1]) < 0.5
    assert out[3][0] > 0.0


def test_stereo_effect_chain_runs_before_sub_split(tmp_path):
    build()
    config = """rate 1000
inputs 2
outputs 4
matrix 0 0 1
matrix 1 1 1
matrix 2 0 0.5
matrix 2 1 0.5
matrix 3 0 0.5
matrix 3 1 0.5
stage_begin 0 stereo-delay native delay
param left_ms 1
param right_ms 3
stage_end
"""
    out = process(tmp_path, config, [1.0, 1.0] + [0.0, 0.0] * 4, 4)
    assert list(out[0][:4]) == [0.0, 1.0, 0.0, 0.0]
    assert list(out[1][:4]) == [0.0, 0.0, 0.0, 1.0]
    assert list(out[2][:4]) == [0.0, 0.5, 0.0, 0.5]
    assert list(out[3][:4]) == [0.0, 0.5, 0.0, 0.5]
