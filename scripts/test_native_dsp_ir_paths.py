#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Real WAV loading through JSON and legacy IR paths at both parser sites."""

import array
import json
import struct
import subprocess
import sys
import wave
from pathlib import Path

from native_test_runner import run_pytest_style_module

ROOT = Path(__file__).resolve().parents[1]
DSP = ROOT / "native_dsp/build/fxroute-dsp-offline"


def write_ir(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(struct.pack("<4h", 16384, 8192, -4096, 2048))


def run_path(tmp_path, kind, token):
    config = tmp_path / "ir-path.conf"
    if kind == "stage":
        directive = ("stage_begin 0 ir native convolver\n"
                     f"param path {token}\nparam dry_db -100\nstage_end\n")
    else:
        directive = f"oconv 0 0 0 -100 0 0 {token}\n"
    config.write_text("rate 48000\ninputs 1\noutputs 1\nmatrix 0 0 1\n" + directive)
    source = tmp_path / "in.f32"
    target = tmp_path / "out.f32"
    source.write_bytes(struct.pack("<64f", 1.0, *([0.0] * 63)))
    result = subprocess.run([str(DSP), str(config), str(source), str(target)],
                            capture_output=True, text=True)
    return result, target


def check_impulse(target):
    output = array.array("f")
    output.frombytes(target.read_bytes())
    assert len(output) == 64
    for index, expected in enumerate((0.5, 0.25, -0.125, 0.0625)):
        assert abs(output[index] - expected) < 2e-5, (index, output[index], expected)
    assert max(abs(value) for value in output[4:]) < 1e-7


def test_ir_paths_decode_json_once_and_preserve_legacy_names(tmp_path):
    subprocess.run([str(ROOT / "native_dsp/build.sh")], check=True)
    names = [
        "ordinary with spaces.wav", "Hörraum 漢字 😀.wav", 'Raum "A".wav',
        r"literal\u00e4\new\test.wav", "controls\b\f\n\r\t\x01.wav",
        "unicode-\u007f\u0080\u07ff\u0800\uffff\U00010000\U0010ffff.wav",
    ]
    failures = []
    cases = 0
    for name in names:
        path = tmp_path / name
        write_ir(path)
        tokens = [json.dumps(str(path)), json.dumps(str(path), ensure_ascii=False)]
        if not any(character in str(path) for character in '\n\r\t\b\f\x01'):
            tokens.append(str(path))  # Old unquoted names must not be unescaped.
        tokens.append(json.dumps(str(path)).replace("/", r"\/"))
        for kind in ("stage", "output"):
            for token in tokens:
                result, target = run_path(tmp_path, kind, token)
                cases += 1
                if result.returncode:
                    failures.append(f"{kind} {token!r}: {result.stderr.strip()}")
                else:
                    check_impulse(target)
    print(f"IR path loading/impulse cases: {cases}")
    assert not failures, "\n".join(failures)


def test_ir_paths_reject_malformed_json_before_file_loading(tmp_path):
    subprocess.run([str(ROOT / "native_dsp/build.sh")], check=True)
    path = tmp_path / "valid.wav"
    write_ir(path)
    prefix = str(path)
    tokens = [
        '""', '"' + prefix, '"' + prefix + '" garbage',
        '"' + prefix + '""', '"' + prefix + '\\"',
        *(json.dumps(prefix)[:-1] + suffix + '"' for suffix in (
            r"\x", r"\u", r"\u123", r"\u12xz", r"\ud800", r"\udc00",
            r"\ud800\u0041", r"\ud800\ud800", r"\udc00\ud800", r"\u0000ignored",
        )),
        '"' + prefix + '\t"',
    ]
    failures = []
    for kind in ("stage", "output"):
        for token in tokens:
            result, _ = run_path(tmp_path, kind, token)
            if result.returncode != 1 or "invalid config line" not in result.stderr:
                failures.append(f"{kind} {token!r}: exit={result.returncode} {result.stderr}")
    assert not failures, "\n".join(failures)


def test_ir_path_capacity_uses_decoded_bytes_and_never_truncates(tmp_path):
    subprocess.run([str(ROOT / "native_dsp/build.sh")], check=True)
    directory = tmp_path
    while len(str(directory).encode()) < 800:
        directory /= "d" * 100
    # Both fixed native path buffers have 1024 bytes, including the terminator.
    path = directory / ("a" * (1023 - len(str(directory).encode()) - 5) + ".wav")
    assert len(str(path).encode()) == 1023
    write_ir(path)
    failures = []
    for kind in ("stage", "output"):
        for token in (str(path), json.dumps(str(path))):
            result, target = run_path(tmp_path, kind, token)
            if result.returncode:
                failures.append(f"{kind} max-size path: {result.stderr}")
            else:
                check_impulse(target)
        # Existing max-size file catches accidental truncation to a valid path.
        for token in (str(path) + "x", json.dumps(str(path) + "x")):
            result, _ = run_path(tmp_path, kind, token)
            if result.returncode != 1 or "invalid config line" not in result.stderr:
                failures.append(f"{kind} overflow: exit={result.returncode} {result.stderr}")
        escaped = tmp_path / ("ü" * 100) / (("ä" * 100) + ".wav")
        write_ir(escaped)
        # JSON can be much longer than the decoded UTF-8 path.
        token = json.dumps(str(escaped)).replace("/", r"\u002f")
        assert len(token) > 1024 and len(str(escaped).encode()) < 1024
        result, target = run_path(tmp_path, kind, token)
        if result.returncode:
            failures.append(f"{kind} escaped expansion: {result.stderr}")
        else:
            check_impulse(target)
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    sys.exit(run_pytest_style_module(globals()))
