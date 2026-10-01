#!/usr/bin/env python3
"""Opt-in PipeWire swap/ownership verification; requires an unused DSP node.

Build the probe from test_engine_workers.c with --wrap=fx_crystalizer_process.
No hardware links, service changes, or global clock metadata writes are made.
"""
import argparse
import array
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import threading
import time


def command(*args):
    return subprocess.check_output(args, text=True)


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise TimeoutError("PipeWire verification did not reach the expected state")


def ports(node, direction):
    graph = json.loads(command("pw-dump"))
    ids = {x["id"] for x in graph if x["type"].endswith(":Node") and
           x.get("info", {}).get("props", {}).get("node.name") == node}
    return [x["info"]["props"]["port.name"] for x in graph if x["type"].endswith(":Port") and
            x.get("info", {}).get("props", {}).get("node.id") in ids and
            x["info"]["props"].get("port.direction") == direction]


def helpers(pid):
    result = {}
    for task in Path(f"/proc/{pid}/task").iterdir():
        name = (task / "comm").read_text().strip()
        if name.startswith("fxdsp-w"):
            stat = (task / "stat").read_text().rsplit(")", 1)[1].split()
            tid = int(task.name)
            result[tid] = dict(name=name, start=stat[19],
                               scheduler=os.sched_getscheduler(tid) & ~0x40000000,
                               priority=os.sched_getparam(tid).sched_priority)
    return result


def events(path, event):
    return path.read_text().count(f"FXDSP_TEST_EVENT {event}\n")


def feed(process):
    block = array.array("f", [((n * 163 + c * 199) % 1021 - 510) * .0001
                              for n in range(1024) for c in range(8)]).tobytes()
    try:
        while process.poll() is None:
            process.stdin.write(block)
            process.stdin.flush()
    except (BrokenPipeError, OSError):
        pass


def stop_processes(processes, timeout=10):
    failures = []
    for process in processes:
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            failures.append(f"Process {process.pid} required forced cleanup")
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as error:
                failures.append(f"Process {process.pid} could not be reaped: {error}")
        except OSError as error:
            failures.append(f"Process {process.pid} cleanup failed: {error}")
    return failures


def exercise(binary, root, rate, swaps, forced=False, blocked=False, probe=False):
    label = f"{binary.name}-{'blocked' if blocked else 'serial' if forced else 'pool'}"
    log_path = root / f"{label}.log"
    control_path, client_path = root / "engine.sock", root / "client.sock"
    entered, release, armed = root / "entered", root / "release", root / "armed"
    for path in (control_path, client_path, entered, release, armed):
        path.unlink(missing_ok=True)
    processes = []
    records = []
    env = dict(os.environ, FXROUTE_DSP_TIMING="1", FXROUTE_DSP_WORKERS="0" if forced else "3",
               PIPEWIRE_QUANTUM=f"1024/{rate}")
    if blocked:
        env.update(FXDSP_TEST_ENTERED=str(entered), FXDSP_TEST_RELEASE=str(release),
                   FXDSP_TEST_ARMED=str(armed))
    with log_path.open("w") as log, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.bind(str(client_path))
        client.settimeout(1.0)  # Production control reply deadline.

        def control(request):
            started = time.monotonic()
            client.sendto(request.encode(), str(control_path))
            reply = client.recv(4096).decode().strip()
            return reply, time.monotonic() - started

        try:
            engine = subprocess.Popen([str(binary), str(root / ("stage.conf" if blocked else "serial.conf")),
                                       str(control_path)], stdout=log, stderr=log, env=env)
            processes.append(engine)
            wait_for(lambda: control_path.exists() and len(ports("fxroute_dsp", "in")) == 8)
            identity = helpers(engine.pid)
            assert len(identity) == (0 if forced else 3), identity
            assert all(t["scheduler"] in (1, 2) and t["priority"] > 0 for t in identity.values()), identity
            reply, _ = control("gain db -30")
            assert reply == "ok", reply
            if probe:
                assert events(log_path, "create") == events(log_path, "acquire") == len(identity)
            source = subprocess.Popen(["pw-cat", "--playback", "--raw", "--format=f32", "--target=0",
                                       f"--rate={rate}", "--channels=8", "--channel-map=" +
                                       ",".join(f"AUX{i}" for i in range(8)), "--latency=1024",
                                       "-P", "node.name=fxroute_swap_source", "-"],
                                      stdin=subprocess.PIPE, stdout=log, stderr=log)
            processes.append(source)
            threading.Thread(target=feed, args=(source,), daemon=True).start()
            meter = subprocess.Popen(["pw-cat", "--record", "--raw", "--format=f32", "--target=0",
                                      f"--rate={rate}", "--channels=2", "--channel-map=AUX0,AUX1",
                                      "--latency=1024", "-P", "node.name=fxroute_swap_meter", "/dev/null"],
                                     stdout=log, stderr=log)
            processes.append(meter)
            wait_for(lambda: len(ports("fxroute_swap_source", "out")) == 8 and
                     len(ports("fxroute_swap_meter", "in")) == 2)
            for i in range(8):
                command("pw-link", f"fxroute_swap_source:output_AUX{i}", f"fxroute_dsp:input_{i+1}")
            for side, i in (("FL", 0), ("FR", 1)):
                command("pw-link", f"fxroute_dsp:post_effect_{side}", f"fxroute_swap_meter:input_AUX{i}")
            if blocked:
                wait_for(lambda: json.loads(control("timing get")[0])["barrier"]["samples"] >= 4)
                armed.touch()
                wait_for(lambda: entered.exists() and entered.read_text().strip().isdigit())
                assert int(entered.read_text()) in identity
                engine.terminate()
                wait_for(lambda: events(log_path, "quit_requested") == 1)
                time.sleep(.15)
                assert engine.poll() is None
                assert not events(log_path, "filter_destroy_end") and not events(log_path, "join")
                destroying = events(log_path, "filter_destroy_begin") == 1
                release.touch()
                engine.wait(timeout=5)
                assert engine.returncode == 0, engine.returncode
                assert events(log_path, "filter_destroy_begin") == events(log_path, "filter_destroy_end") == 1
                text = log_path.read_text()
                assert text.index("FXDSP_TEST_EVENT filter_destroy_end") < text.index("FXDSP_TEST_EVENT join")
                records.append(dict(active_worker_tid=int(entered.read_text()), shutdown_drained=True,
                                    filter_destroy_entered_while_blocked=destroying))
            else:
                for iteration in range(swaps):
                    kind = ("serial", "stage", "bank")[iteration % 3]
                    reply, elapsed = control(f"swap config {root / (kind + '.conf')} -30")
                    assert reply == "ok" and elapsed < 1.0, (kind, iteration, reply, elapsed)
                    reply, _ = control("timing reset")
                    assert reply == "ok", reply
                    time.sleep(.04)
                    reply, _ = control("timing get")
                    timing = json.loads(reply)
                    assert timing["workers"] == len(identity), timing
                    assert timing["worker_jobs"] == (1 if kind == "serial" else 8), timing
                    assert timing["worker_used"] == (0 if forced or kind == "serial" else 3), timing
                    assert timing["callback"]["samples"] > 0, timing
                    if not forced and kind != "serial":
                        assert timing["barrier"]["samples"] > 0 and timing["wake"]["samples"] > 0, timing
                    assert helpers(engine.pid) == identity
                    if probe:
                        assert events(log_path, "create") == events(log_path, "acquire") == len(identity)
                        assert not events(log_path, "join")
                    records.append(dict(graph=kind, swap_seconds=elapsed, timing=timing))
                    (root / f"{label}.json").write_text(json.dumps(records, indent=2))
                reply, _ = control("swap config /not/a/config -30")
                assert reply.startswith("error") and helpers(engine.pid) == identity
            result = dict(label=label, helpers=identity, records=records,
                          binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
        finally:
            release.touch()
            try:
                failures = stop_processes(processes)  # Keep transport active until the engine drains.
            finally:
                for path in (control_path, client_path):
                    path.unlink(missing_ok=True)
            assert not failures, failures
            if processes:
                assert processes[0].returncode == 0, log_path.read_text()
    if probe:
        counts = json.loads(re.search(r"FXDSP_TEST_COUNTS (\{.*\})", log_path.read_text())[1])
        assert counts == dict(created=len(identity), admitted=len(identity), joined=len(identity)), counts
        result["counts"] = counts
    return result


def write_configs(root, rate):
    payload = array.array("f", [.25 if n == 0 else .0001 for n in range(32825)]).tobytes()
    ir = root / "room.wav"
    ir.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(payload)) + b"WAVEfmt " +
                   struct.pack("<IHHIIHH", 16, 3, 1, rate, rate * 4, 4, 32) +
                   b"data" + struct.pack("<I", len(payload)) + payload)
    base = [f"rate {rate}", "inputs 8", "outputs 8"] + [f"matrix {c} {c} 1" for c in range(8)]
    for kind in ("serial", "stage", "bank"):
        lines = list(base)
        if kind == "bank":
            lines += [f'oconv {c} 0 0 -100 0 0 "{ir}"' for c in range(8)]
        if kind == "stage":
            lines += ["stage_begin 0 conv native convolver", f'param path "{ir}"', "stage_end"]
        if kind != "serial":
            lines += [f"stage_begin {1 if kind == 'stage' else 0} crystal native crystalizer", "stage_end"]
        (root / f"{kind}.conf").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--probe-engine", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--rate", type=int, default=48000)
    parser.add_argument("--swaps", type=int, default=30)
    args = parser.parse_args()
    assert not ports("fxroute_dsp", "in"), "Stop the product engine before running this opt-in test"
    root = args.evidence.resolve()
    root.mkdir(parents=True, exist_ok=True)
    write_configs(root, args.rate)
    offline = args.engine.resolve().with_name(args.engine.name + "-offline")
    for kind in ("serial", "stage", "bank"):
        subprocess.run([str(offline), str(root / f"{kind}.conf"), "/dev/null", "/dev/null"],
                       check=True, stdout=subprocess.DEVNULL)
    results = []
    try:
        for binary, probe in ((args.engine.resolve(), False), (args.probe_engine.resolve(), True)):
            for forced in (False, True):
                results.append(exercise(binary, root, args.rate, args.swaps, forced=forced, probe=probe))
                (root / "results.json").write_text(json.dumps(results, indent=2))
        results.append(exercise(args.probe_engine.resolve(), root, args.rate, 0, blocked=True, probe=True))
        print(f"PASS: {args.swaps * 4} one-second swaps, stable RT helpers, no new admissions, active-job shutdown")
    finally:
        (root / "results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
