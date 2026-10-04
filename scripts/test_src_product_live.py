#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in acceptance through real FXRoute APIs, adapters, DSP and hardware links.

Run on the audio host with FXROUTE_SRC_LIVE=1. Provider media is selected from
the existing library/favorites/stations, never synthesized as a substitute.
"""
import array
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from audio.src_policy import client_env
from dsp.runtime import iter_pw_links
from test_stdin_audio_live import _main_dsp_outputs

BASE = os.environ.get("FXROUTE_BASE_URL", "http://127.0.0.1:8000")


def http(path, body=None):
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        error.add_note(error.read().decode(errors="replace"))
        raise


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("Product path did not become ready")


def player_property(name):
    """Read paused-position truth independently of delayed status observers."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(os.environ.get("FXROUTE_MPV_SOCKET", "/tmp/mpv.sock"))
        connection.sendall((json.dumps({"command": ["get_property", name], "request_id": 1}) + "\n").encode())
        with connection.makefile("r") as responses:
            for line in responses:
                response = json.loads(line)
                if response.get("request_id") == 1:
                    assert response.get("error") == "success", response
                    return response.get("data")
    raise AssertionError("MPV property read returned no response")


def paused_position(timeout=5):
    """Wait for queued audio timing to freeze after the pause acknowledgement."""
    deadline = time.monotonic() + timeout
    previous = None
    stable = 0
    while time.monotonic() < deadline:
        assert player_property("pause") is True
        position = player_property("time-pos")
        if isinstance(position, (int, float)):
            stable = stable + 1 if previous is not None and abs(position - previous) < 0.001 else 0
            if stable >= 3:
                return position
            previous = position
        time.sleep(0.1)
    raise AssertionError("Paused MPV position did not freeze")


def graph(mode):
    src = http("/api/audio/src")
    assert src["available"] and src["mode"] == mode, src
    configured = [n for n in src["effective_nodes"] if n["mode"] != "unconfigured"]
    assert configured and all(n["mode"] == mode and not n["error"] for n in configured), configured
    outputs = http("/api/audio/outputs")
    sink = (outputs.get("selected_output") or {}).get("target_name")
    assert sink and sink != "fxroute_dsp_sink", "Selected hardware disappeared"
    links = list(iter_pw_links(subprocess.check_output(["pw-link", "-l"], text=True)))
    assert any(s.startswith("fxroute_dsp:output_") and t.startswith(sink + ":") for s, t in links)
    assert not any(not s.startswith("fxroute_dsp:") and t.startswith(sink + ":") for s, t in links), "DSP bypass link"
    return src, sink


def capture_start(directory, label, sink):
    left, right, _, _ = _main_dsp_outputs(sink)
    path = directory / (label + ".f32")
    log = (directory / (label + "-capture.log")).open("w")
    name = "fxroute_src_acceptance_capture"
    process = subprocess.Popen(["pw-cat", "--record", "--raw", "--format", "f32", "--rate", "48000",
        "--channels", "2", "--target", "0", "--properties", f"node.name={name} node.autoconnect=false", str(path)],
        env=client_env(), stdout=log, stderr=log)
    wait_for(lambda: name + ":input_FL" in subprocess.check_output(["pw-link", "-i"], text=True))
    for source, side in ((left, "FL"), (right, "FR")):
        subprocess.run(["pw-link", source, name + ":input_" + side], check=True)
    return process, log, path


def capture_finish(process, log, path, expected_seconds=None):
    process.send_signal(2)
    process.wait(timeout=10)
    log.close()
    samples = array.array("f")
    samples.frombytes(path.read_bytes())
    assert samples and all(math.isfinite(s) for s in samples), "No finite post-DSP PCM"
    rms = math.sqrt(sum(s*s for s in samples) / len(samples))
    assert rms > 1e-7, "Post-DSP capture is silent"
    peak = max(abs(s) for s in samples)
    active = [i for i in range(len(samples)//2) if max(abs(samples[i*2]), abs(samples[i*2+1])) > peak*0.005]
    span = active[-1]-active[0]+1
    if expected_seconds is not None:
        assert span >= expected_seconds*48000*0.98, f"Finite stream was truncated: {span} frames"
    return {"post_dsp_frames": len(samples)//2, "post_dsp_rms": rms, "active_span_frames": span}


def tones(rate, channels, format_name, seconds=4):
    floats = array.array("f", (0.015 * math.sin(2*math.pi*(701+137*c)*i/rate)
                               for i in range(round(rate*seconds)) for c in range(channels)))
    if format_name == "s24le":
        return b"".join(round(s*8388608).to_bytes(3,"little",signed=True) for s in floats)
    return floats.tobytes()


def run_finite(command, payload, environment, mode, directory, label, seconds):
    path = directory / (mode + "-" + label + "-input.raw")
    path.write_bytes(payload)
    with path.open("rb") as source:
        child = subprocess.Popen(command, stdin=source, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            observed = []
            if seconds >= 2:
                time.sleep(1.5)
                deadline = time.monotonic() + 1.5
                while True:
                    try:
                        src, _ = graph(mode)
                        break
                    except AssertionError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.1)
                observed = src["effective_nodes"]
                assert any(n["state"] == "running" and n["input_rate"] != n["output_rate"]
                           and n["output_rate"] == 48000 for n in observed), "No effective unequal-rate converter"
            stdout, stderr = child.communicate(timeout=25)
            (directory / (mode + "-" + label + "-transport.log")).write_bytes(stdout + stderr)
            metrics = [json.loads(item) for item in re.findall(r"FXSRC_WORKER_STATS (\{.*\})", stderr.decode(errors="replace"))]
            (directory / (mode + "-" + label + "-metrics.json")).write_text(json.dumps(metrics, indent=2))
            assert child.returncode == 0, stderr.decode()
            return observed
        finally:
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=5)


def main():
    if os.environ.get("FXROUTE_SRC_LIVE") != "1":
        print("SKIP: real product SRC acceptance requires FXROUTE_SRC_LIVE=1 on the audio host")
        return
    directory = Path(os.environ.get("FXROUTE_SRC_EVIDENCE_DIR", "/tmp/opencode/product-src-live"))
    directory.mkdir(parents=True, exist_ok=True)
    saved = {p: http(p) for p in ("/api/audio/source-mode", "/api/audio/samplerate", "/api/audio/src", "/api/status")}
    assert not saved["/api/status"]["playing"], "Stop playback before live acceptance"
    (directory / "before.json").write_text(json.dumps(saved, indent=2))
    armed = saved["/api/audio/source-mode"].get("auto_select", {}).get("armed", [])
    records = []
    unavailable = []
    modes = os.environ.get("FXROUTE_SRC_LIVE_MODES", "low,standard,high").split(",")
    assert modes and set(modes) <= {"low", "standard", "high"}
    selected_paths = set(filter(None, os.environ.get("FXROUTE_SRC_LIVE_PATHS", "").split(",")))
    tracks = http("/api/tracks")
    stations = http("/api/stations")
    provider = http("/api/streaming/tidal/status")
    tidal = []
    if provider.get("authenticated"):
        try:
            ids = http("/api/streaming/tidal/favorites/ids").get("tracks", [])
            tidal = ids[:1]
        except (OSError, ValueError) as error:
            unavailable.append({"path": "TIDAL", "reason": str(error)})
    if not tidal:
        unavailable.append({"path": "TIDAL", "reason": "No accessible authenticated favorite track"})
    try:
        for key in armed:
            http("/api/audio/source-auto", {"inputKey": key, "enabled": False})
        http("/api/audio/source-mode", {"mode": "app-playback"})
        http("/api/audio/samplerate", {"mode": "fixed", "rate": 48000})
        for mode in modes:
            http("/api/stop", {})
            http("/api/audio/src", {"mode": mode})
            src, sink = graph(mode)
            paths = [("Library", "local", tracks[0]["id"])] if tracks else []
            paths += [("TIDAL", "tidal", str(tidal[0]))] if tidal else []
            paths += [("Radio", "radio", stations[0]["id"])] if stations else []
            for label, source, track in paths:
                if selected_paths and label not in selected_paths:
                    continue
                capture, log, pcm = capture_start(directory, mode + "-" + label, sink)
                try:
                    http("/api/play", {"source": source, "track_id": track})
                    wait_for(lambda: http("/api/status").get("playing"), 45)
                    time.sleep(5)
                    observed, _ = graph(mode)
                    assert any(n["name"] == "mpv" and n["mode"] == mode and n["input_rate"] > 0
                               and n["output_rate"] == 48000 for n in observed["effective_nodes"]), "MPV effective SRC state missing"
                    record = {"mode": mode, "path": label, "nodes": observed["effective_nodes"]}
                    record.update(capture_finish(capture, log, pcm))
                    other_mode = {"low": "standard", "standard": "high", "high": "low"}[mode]
                    before = http("/api/status")
                    applied = http("/api/audio/src", {"mode": other_mode})
                    assert applied["mode"] == other_mode and applied["available"], applied
                    resumed = wait_for(lambda: (state if (state := http("/api/status")).get("playing") else None), 45)
                    assert not resumed["paused"] and resumed["current_file"] == before["current_file"], resumed
                    assert resumed["current_track"]["id"] == before["current_track"]["id"], resumed
                    assert resumed["queue"] == before["queue"], (before["queue"], resumed["queue"])
                    if source != "radio":
                        assert abs(resumed["position"] - before["position"]) < 3, (before, resumed)
                    _, sink = graph(other_mode)
                    capture, log, pcm = capture_start(directory, mode + "-" + label + "-resumed", sink)
                    time.sleep(3)
                    record["resumed_pcm"] = capture_finish(capture, log, pcm)
                    http("/api/playback/toggle", {})
                    paused = http("/api/status")
                    assert paused["paused"], "Paused SRC test did not pause the owned track"
                    assert player_property("pause") is True
                    paused["position"] = paused_position()
                    restored = http("/api/audio/src", {"mode": mode})
                    assert restored["mode"] == mode and restored["available"], restored
                    after = http("/api/status")
                    assert not after["playing"] and after["paused"] and after["current_file"] == paused["current_file"], after
                    assert player_property("pause") is True
                    assert after["queue"] == paused["queue"], (paused["queue"], after["queue"])
                    if source != "radio":
                        restored_position = player_property("time-pos")
                        assert abs(restored_position - paused["position"]) < 0.5, (paused["position"], restored_position)
                    _, sink = graph(mode)
                    record["live_apply"] = {"playing_to": other_mode, "paused_to": mode, "automatic_resume": True,
                                            "before_position": before["position"], "resumed_position": resumed["position"],
                                            "paused_position": paused["position"],
                                            "restored_position": player_property("time-pos"), "queue_preserved": True}
                    records.append(record)
                    print("PASS", mode, label, flush=True)
                finally:
                    if capture.poll() is None:
                        capture.terminate();capture.wait(timeout=5);log.close()
            for label, rate, channels, left, right, fmt, seconds in (
                    ("STDIN", 44100, 2, 1, 2, "s24le", 4),
                    ("STDIN-short", 44100, 2, 1, 2, "f32le", 128/44100),
                    ("STDIN-32", 96000, 32, 29, 30, "f32le", 4)):
                if selected_paths and label not in selected_paths:
                    continue
                http("/api/audio/source-mode", {"mode": "stdin-input"})
                capture, log, pcm = capture_start(directory, mode + "-" + label, sink)
                try:
                    command = [sys.executable, str(ROOT / "scripts/fxroute_stdin.py"), "stdin", "--format", fmt,
                        "--rate", str(rate), "--channels", str(channels), "--left", str(left), "--right", str(right)]
                    observed = run_finite(command, tones(rate, channels, fmt, seconds), None, mode, directory, label, seconds)
                    time.sleep(0.25)
                    record = {"mode": mode, "path": label, "native_rate": rate, "channels": channels,
                              "drained": True, "nodes": observed, **capture_finish(capture, log, pcm, seconds)}
                    records.append(record)
                    print("PASS", mode, label, flush=True)
                finally:
                    if capture.poll() is None:
                        capture.terminate();capture.wait(timeout=5);log.close()
                    http("/api/audio/source-mode", {"mode": "app-playback"})
            for label, executable in (("Native", "pw-cat"), ("Pulse", "pacat"), ("ALSA-PipeWire", "aplay")):
                if selected_paths and label not in selected_paths:
                    continue
                if not shutil.which(executable):
                    unavailable.append({"path": label, "reason": executable + " is not installed"})
                    continue
                capture, log, pcm = capture_start(directory, mode + "-" + label, sink)
                try:
                    if label == "Native":
                        command = [executable, "--playback", "--raw", "--target", "fxroute_dsp_sink", "--format", "f32",
                                   "--rate", "44100", "--channels", "2", "-"]
                    elif label == "Pulse":
                        command = [executable, "--playback", "--raw", "--device=fxroute_dsp_sink", "--format=float32le", "--rate=44100", "--channels=2"]
                    else:
                        command = [executable, "-D", "pipewire", "-t", "raw", "-f", "FLOAT_LE", "-r", "44100", "-c", "2"]
                    environment = {k:v for k,v in os.environ.items() if k != "SPA_PLUGIN_DIR"}
                    if label == "ALSA-PipeWire":
                        environment["PIPEWIRE_NODE"] = "fxroute_dsp_sink"
                    observed = run_finite(command, tones(44100,2,"f32le"), environment, mode, directory, label, 4)
                    time.sleep(0.25)
                    record = {"mode": mode, "path": label, "nodes": observed, **capture_finish(capture, log, pcm, 4)}
                    records.append(record)
                    print("PASS", mode, label, flush=True)
                finally:
                    if capture.poll() is None:
                        capture.terminate();capture.wait(timeout=5);log.close()
            (directory / "results.json").write_text(json.dumps({"passed": records, "unverified": unavailable}, indent=2))
    finally:
        original = saved["/api/audio/source-mode"]
        restores = [
            ("/api/stop", {}), ("/api/audio/source-mode", {"mode": "app-playback"}),
            ("/api/audio/src", {"mode": saved["/api/audio/src"]["mode"]}),
            ("/api/audio/samplerate", saved["/api/audio/samplerate"]["policy"]),
            ("/api/audio/source-mode", {"mode": original["mode"], "inputKey": (original.get("selected_input") or {}).get("key")}),
            *[("/api/audio/source-auto", {"inputKey": key, "enabled": True}) for key in armed],
        ]
        failures = []
        for path, body in restores:
            try:
                http(path, body)
            except Exception as error:
                failures.append(error)
        if failures:
            raise ExceptionGroup("Product acceptance cleanup failed", failures)


if __name__ == "__main__":
    main()
