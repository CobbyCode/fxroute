#!/usr/bin/env python3
import array
import math
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DSP = ROOT / "native_dsp" / "build" / "fxroute-dsp-offline"


def test_peq_types_and_lr24_crossover_sum(tmp_path):
    subprocess.run([str(ROOT / "native_dsp" / "build.sh")], check=True)
    cfg = tmp_path / "dsp.conf"
    cfg.write_text("""rate 48000
inputs 1
outputs 2
matrix 0 0 1
matrix 1 0 1
peq 0 lowpass 1000 0.70710678 0
peq 0 lowpass 1000 0.70710678 0
peq 1 highpass 1000 0.70710678 0
peq 1 highpass 1000 0.70710678 0
""")
    frames = 48000
    source = tmp_path / "in.f32"
    target = tmp_path / "out.f32"
    values = array.array("f", (math.sin(2 * math.pi * 1000 * i / 48000) for i in range(frames)))
    values.tofile(source.open("wb"))
    subprocess.run([str(DSP), str(cfg), str(source), str(target)], check=True)
    output = array.array("f")
    output.fromfile(target.open("rb"), target.stat().st_size // 4)
    low = output[8000::2]
    high = output[8001::2]
    low_rms = math.sqrt(sum(x * x for x in low) / len(low))
    high_rms = math.sqrt(sum(x * x for x in high) / len(high))
    assert 0.33 < low_rms < 0.38
    assert 0.33 < high_rms < 0.38

    for kind in ("bell", "notch", "lowshelf", "highshelf"):
        text = cfg.read_text() + f"peq 0 {kind} 2000 0.8 3\n"
        cfg.write_text(text)
        subprocess.run([str(DSP), str(cfg), str(source), str(target)], check=True)


from native_test_runner import run_pytest_style_module

if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
