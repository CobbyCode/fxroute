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
output 0 0 2 normal
delay_add 0 3
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
delay_add 0 1001
"""
    cfg = tmp_path / "dsp.conf"
    source = tmp_path / "in.f32"
    target = tmp_path / "out.f32"
    cfg.write_text(config)
    array.array("f", [1.0]).tofile(source.open("wb"))
    result = subprocess.run([str(DSP), str(cfg), str(source), str(target)])
    assert result.returncode != 0
