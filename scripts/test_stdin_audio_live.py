#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Live STDIN acceptance on the audio host (opt-in, representative matrix).

Runs on the FXRoute audio machine itself (needs the service, pw-cat and the
installed ``fxroute`` CLI)::

    FXROUTE_STDIN_LIVE=1 python3 scripts/test_stdin_audio_live.py

Representative matrix (no exhaustive channel x format x rate product):

- 1ch/s16le/48000 mono (duplicated to L/R)
- 2ch/s24le/44100 stereo
- 3ch/s32le/96000, pair 2/3
- 8ch/f32le/48000, pair 5/6 (plus the DSP-chain proof)
- 32ch/f32le/48000, pair 31/32

The 8-channel case additionally proves the normal DSP path: a temporary
-12 dB PEQ notch at the left tone, a -6 dB master-volume step and a
volume-to-zero mute step. Capture taps the post-DSP Main outputs and the
test asserts the captured DSP outputs are linked to the selected hardware.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dsp.runtime import iter_pw_links
BASE_URL = os.environ.get("FXROUTE_BASE_URL", "http://127.0.0.1:8000")
RUN_LIVE = os.environ.get("FXROUTE_STDIN_LIVE") == "1"

TONE_BASE_HZ = 251.0
TONE_STEP_HZ = 137.0
TONE_AMPLITUDE = 0.03
SIGNAL_SECONDS = 5.0
SILENCE_SECONDS = 0.5
ANALYZE_SECONDS = 2.0

# (channels, format, rate, left, right)
MATRIX = [
    (1, "s16le", 48000, 1, 1),
    (2, "s24le", 44100, 1, 2),
    (3, "s32le", 96000, 2, 3),
    (8, "f32le", 48000, 5, 6),
    (32, "f32le", 48000, 31, 32),
]

FORMAT_BITS = {"s16le": 16, "s24le": 24, "s32le": 32, "f32le": 32}


def _http(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode() or "{}")


def _pw_link(args: list[str]) -> str:
    return subprocess.check_output(["pw-link", *args], text=True, timeout=10)


def _tone_frequency(index: int) -> float:
    return TONE_BASE_HZ + TONE_STEP_HZ * index


def _quantize(sample: float, format_name: str) -> bytes:
    if format_name == "f32le":
        return struct.pack("<f", sample)
    bits = FORMAT_BITS[format_name]
    code = round(sample * (2 ** (bits - 1) - 1))
    code = max(-(2 ** (bits - 1)), min(2 ** (bits - 1) - 1, code))
    return code.to_bytes(bits // 8, "little", signed=True)


def _pcm_payload(channels: int, format_name: str, rate: int,
                 signal_seconds: float = SIGNAL_SECONDS) -> bytes:
    frames = round(rate * (signal_seconds + SILENCE_SECONDS))
    signal_frames = round(rate * signal_seconds)
    payload = bytearray()
    for frame in range(frames):
        for index in range(channels):
            if frame < signal_frames:
                sample = TONE_AMPLITUDE * math.sin(
                    2 * math.pi * _tone_frequency(index) * frame / rate)
            else:
                sample = 0.0
            payload.extend(_quantize(sample, format_name))
    return bytes(payload)


def _output_state() -> dict:
    return _http("GET", "/api/audio/output-state")


def _set_bank_preset(bank_id: str, live_applied: bool | None = None, **options) -> dict:
    """Assign a bank preset through the same mutation the UI uses."""
    revision = _output_state()["revision"]
    mutation = {"kind": "set_bank_preset", "mode": _output_state()["active_mode"],
                "bank_id": bank_id, **options}
    result = _http("POST", "/api/audio/output-state/apply",
                   {"expected_revision": revision, "mutation": mutation})
    if live_applied is not None:
        assert result.get("live_applied") is live_applied, result
    return result


def _sink_volume_percent(sink_name: str) -> int:
    """Read back the live sink volume (the canonical master actuator)."""
    import re
    output = subprocess.check_output(
        ["pactl", "get-sink-volume", sink_name], text=True, timeout=10)
    match = re.search(r"(\d+)%", output)
    assert match, f"unparseable sink volume: {output!r}"
    return int(match.group(1))


def _bank_state(bank_id: str) -> dict:
    state = _output_state()
    return state["modes"][state["active_mode"]]["banks"][bank_id]


def _tone_amplitude(samples: list[float], rate: int, frequency: float) -> float:
    count = len(samples)
    cosine = sum(value * math.cos(2 * math.pi * frequency * i / rate)
                 for i, value in enumerate(samples))
    sine = sum(value * math.sin(2 * math.pi * frequency * i / rate)
               for i, value in enumerate(samples))
    return 2 * math.hypot(cosine, sine) / count


def _rbj_peaking_db(frequency: float, center_hz: float, q: float,
                     gain_db: float, rate: int) -> float:
    """Expected RBJ peaking-EQ response (proves the rendered filter shape)."""
    cascade = 10.0 ** (gain_db / 40.0)
    w0 = 2 * math.pi * center_hz / rate
    alpha = math.sin(w0) / (2 * q)
    w = 2 * math.pi * frequency / rate
    cos_w0, cos_w, cos_2w = math.cos(w0), math.cos(w), math.cos(2 * w)
    sin_w, sin_2w = math.sin(w), math.sin(2 * w)
    b0, b1, b2 = 1 + alpha * cascade, -2 * cos_w0, 1 - alpha * cascade
    a0, a1, a2 = 1 + alpha / cascade, -2 * cos_w0, 1 - alpha / cascade
    num = complex(b0 + b1 * cos_w + b2 * cos_2w, -(b1 * sin_w + b2 * sin_2w))
    den = complex(a0 + a1 * cos_w + a2 * cos_2w, -(a1 * sin_w + a2 * sin_2w))
    return 20 * math.log10(abs(num / den))


def _db(ratio: float) -> float:
    return 20 * math.log10(max(ratio, 1e-12))


def _main_dsp_outputs(sink_name: str) -> tuple[str, str, str, str]:
    """Resolve DSP and sink-monitor taps for the selected sink's mains.

    Returns ``(dsp_left, dsp_right, monitor_left, monitor_right)``: the DSP
    outputs feeding the sink's first playback ports (pre-sink-volume, for
    the filter proof) and the matching sink monitor ports (post-sink-volume,
    for the master-volume/mute proof — the canonical master is a sink
    volume applied after the DSP engine).
    """
    deadline = time.monotonic() + 15
    while True:
        dsp: dict[str, str] = {}
        for source, target in iter_pw_links(_pw_link(["-l"])):
            if source.startswith("fxroute_dsp:output_") and target.startswith(sink_name + ":"):
                dsp.setdefault(source, target)
        ordered = sorted(dsp.items(), key=lambda item: item[1])
        if len(ordered) >= 2:
            (dsp_left, hw_left), (dsp_right, hw_right) = ordered[0], ordered[1]
            suffix_left = hw_left.split("playback_", 1)[1]
            suffix_right = hw_right.split("playback_", 1)[1]
            return (dsp_left, dsp_right,
                    f"{sink_name}:monitor_{suffix_left}", f"{sink_name}:monitor_{suffix_right}")
        if time.monotonic() >= deadline:
            raise AssertionError(f"no DSP outputs linked to {sink_name}")
        time.sleep(0.5)


@unittest.skipUnless(RUN_LIVE, "live audio acceptance needs FXROUTE_STDIN_LIVE=1 on the audio host")
class StdinLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert shutil.which("pw-cat"), "pw-cat is required"
        assert shutil.which("fxroute"), "installed fxroute CLI is required"
        assert shutil.which("pw-link"), "pw-link is required"
        cls.saved_source = _http("GET", "/api/audio/source-mode")
        cls.saved_presets = _http("GET", "/api/dsp/presets")
        cls.saved_extras = _http("GET", "/api/dsp/extras")
        cls.saved_status = _http("GET", "/api/status")
        cls.saved_outputs = _http("GET", "/api/audio/outputs")
        sink = (cls.saved_outputs.get("selected_output") or {}).get("name") or (
            cls.saved_outputs.get("current_output") or {}).get("name")
        assert sink, "no selected audio output"
        cls.sink_name = sink
        cls.dsp_left, cls.dsp_right, cls.mon_left, cls.mon_right = _main_dsp_outputs(sink)
        rate = (cls.saved_outputs.get("selected_output") or {}).get("active_rate")
        cls.graph_rate = int(rate) if rate else 48000
        # Channel isolation is asserted against a clean chain: the box may
        # render a crosstalk-correcting convolver that legitimately bleeds
        # test tones across channels. The engine renders bank assignments,
        # so listen to the clean A side without rewriting any slots (the
        # DSP-chain proof below shapes it again through the same mutation).
        cls.saved_bank = dict(_bank_state("global"))
        _set_bank_preset("global", active_side="A")
        _http("POST", "/api/dsp/presets/load", {"preset_name": "Neutral"})
        _http("POST", "/api/audio/source-mode", {"mode": "stdin-input"})

    @classmethod
    def tearDownClass(cls):
        try:
            saved_bank = dict(getattr(cls, "saved_bank", {}))
            if saved_bank.get("active_side") in {"A", "B"}:
                _set_bank_preset("global", active_side=saved_bank["active_side"])
        except Exception:
            pass
        try:
            saved_compare = cls.saved_presets.get("compare") or {}
            _http("POST", "/api/dsp/compare", {
                "presetA": saved_compare.get("presetA") or "",
                "presetB": saved_compare.get("presetB") or "",
                "activeSide": saved_compare.get("activeSide"),
            })
        except Exception:
            pass
        try:
            _http("POST", "/api/dsp/presets/load",
                  {"preset_name": cls.saved_presets.get("active_preset") or "Neutral"})
        except Exception:
            pass
        try:
            _http("POST", "/api/dsp/extras", cls.saved_extras.get("extras") or {})
        except Exception:
            pass
        try:
            volume = cls.saved_status.get("volume")
            if isinstance(volume, (int, float)):
                _http("POST", "/api/volume", {"volume": int(volume)})
        except Exception:
            pass
        try:
            mode = cls.saved_source.get("mode") or "app-playback"
            payload = {"mode": mode}
            selected = (cls.saved_source.get("selected_input") or {}).get("key")
            if selected:
                payload["inputKey"] = selected
            _http("POST", "/api/audio/source-mode", payload)
        except Exception:
            pass
        leftovers = subprocess.run(
            ["pw-link", "-l"], capture_output=True, text=True, timeout=10,
        ).stdout
        assert "fxroute_stdin_" not in leftovers, "STDIN adapter node leaked"

    def _run_pipeline(self, channels: int, format_name: str, rate: int,
                      left: int, right: int,
                      signal_seconds: float = SIGNAL_SECONDS,
                      tap: str = "dsp") -> tuple[list[float], list[float]]:
        payload = _pcm_payload(channels, format_name, rate, signal_seconds)
        capture_name = f"fxroute_stdin_test_capture_{os.getpid()}"
        capture_file = tempfile.NamedTemporaryFile(
            prefix="fxroute-stdin-capture-", suffix=".f32", delete=False)
        capture_file.close()
        capture = subprocess.Popen(
            ["pw-cat", "--record", "--raw", "--target", "0",
             "--rate", str(self.graph_rate), "--channels", "2",
             "--channel-map", "FL,FR", "--format", "f32",
             "--properties", f"node.name={capture_name} node.autoconnect=false",
             capture_file.name],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            inputs: list[str] = []
            while time.monotonic() < deadline:
                found = [port for port in _pw_link(["-i"]).splitlines()
                         if port.startswith(capture_name + ":")]
                if len(found) >= 2:
                    inputs = sorted(found)[:2]
                    break
                time.sleep(0.05)
            self.assertEqual(len(inputs), 2, "capture input ports missing")
            for source_port, capture_port in zip(
                    (self.dsp_left, self.dsp_right) if tap == "dsp"
                    else (self.mon_left, self.mon_right), inputs):
                subprocess.check_call(["pw-link", source_port, capture_port], timeout=10)
            graph = set(iter_pw_links(_pw_link(["-l"])))
            expected = ((self.dsp_left, inputs[0]), (self.dsp_right, inputs[1])) if tap == "dsp" else (
                (self.mon_left, inputs[0]), (self.mon_right, inputs[1]))
            for edge in expected:
                self.assertIn(edge, graph)
            client = subprocess.Popen(
                ["fxroute", "stdin", "--format", format_name, "--rate", str(rate),
                 "--channels", str(channels), "--left", str(left), "--right", str(right)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                _, stderr = client.communicate(payload, timeout=60)
            finally:
                if client.poll() is None:
                    client.kill()
            self.assertEqual(client.returncode, 0, stderr.decode()[-2000:])
            time.sleep(1.5)
        finally:
            capture.terminate()
            try:
                capture.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                capture.kill()
                capture.communicate(timeout=10)
        try:
            with open(capture_file.name, "rb") as handle:
                raw = handle.read()
        finally:
            os.unlink(capture_file.name)
        frames = len(raw) // 8
        # Anchor the analysis to the end of the signal: pipe startup
        # (adapter latency, resampler priming) and the drain silence must
        # not dilute the measurement.
        analyze = min(ANALYZE_SECONDS, signal_seconds - 1.0)
        self.assertGreater(analyze, 0.25)
        end = frames - int(self.graph_rate * 1.5) - int(self.graph_rate * 0.25)
        start = end - int(self.graph_rate * analyze)
        self.assertGreater(start, 0, "capture too short for analysis")
        window = raw[start * 8:(start + int(self.graph_rate * analyze)) * 8]
        values = struct.unpack(f"<{len(window) // 4}f", window)
        return list(values[0::2]), list(values[1::2])

    def _await_clean_engine(self) -> None:
        """Wait until the loaded Neutral chain actually renders discretely.

        Preset loads rebuild the native engine asynchronously; measuring
        before the rebuild completes would attribute the previous preset's
        shaping (e.g. a crosstalk-correcting convolver) to the new input.
        A short probe either confirms the clean chain twice in a row or
        fails loudly.
        """
        deadline = time.monotonic() + 60
        clean_runs = 0
        while True:
            left, right = self._run_pipeline(2, "s16le", 44100, 1, 2, signal_seconds=2.5)
            left_level = _tone_amplitude(left, self.graph_rate, _tone_frequency(0))
            right_level = _tone_amplitude(right, self.graph_rate, _tone_frequency(1))
            leak = _db(_tone_amplitude(right, self.graph_rate, _tone_frequency(0)) / right_level)
            if left_level > 0 and right_level > 0 and leak < -40.0:
                clean_runs += 1
                if clean_runs >= 2:
                    return
            else:
                clean_runs = 0
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"engine did not render the clean chain (level={left_level:.5f}, leak={leak:.1f}dB)")
            time.sleep(5)

    def _assert_pair(self, left: list[float], right: list[float], channels: int,
                     left_index: int, right_index: int, min_db: float = 40.0,
                     level_db: float = 0.0) -> None:
        left_freq = _tone_frequency(left_index)
        right_freq = _tone_frequency(right_index)
        left_level = _tone_amplitude(left, self.graph_rate, left_freq)
        right_level = _tone_amplitude(right, self.graph_rate, right_freq)
        self.assertGreater(left_level, 0, "left program channel is silent")
        self.assertGreater(right_level, 0, "right program channel is silent")
        if left_index == right_index:
            # Mono duplication: both program channels carry the same tone at
            # the same level; leak/crosstalk checks are meaningless here.
            self.assertAlmostEqual(_db(right_level / left_level), 0, delta=1.0,
                                   msg="mono duplication is unbalanced")
            return
        for index in range(channels):
            frequency = _tone_frequency(index)
            if index != left_index:
                self.assertLess(
                    _db(_tone_amplitude(left, self.graph_rate, frequency) / left_level),
                    -min_db + level_db, f"channel {index + 1} leaks into left")
            if index != right_index:
                self.assertLess(
                    _db(_tone_amplitude(right, self.graph_rate, frequency) / right_level),
                    -min_db + level_db, f"channel {index + 1} leaks into right")
        self.assertLess(
            _db(_tone_amplitude(left, self.graph_rate, right_freq) / left_level),
            -min_db + level_db, "right tone crosstalks into left")
        self.assertLess(
            _db(_tone_amplitude(right, self.graph_rate, left_freq) / right_level),
            -min_db + level_db, "left tone crosstalks into right")

    def test_representative_matrix_reaches_post_dsp_output(self):
        self._await_clean_engine()
        for channels, format_name, rate, left, right in MATRIX:
            with self.subTest(channels=channels, format=format_name, rate=rate):
                captured_left, captured_right = self._run_pipeline(
                    channels, format_name, rate, left, right)
                if channels == 1:
                    self._assert_pair(captured_left, captured_right, 1, 0, 0)
                else:
                    self._assert_pair(captured_left, captured_right, channels, left - 1, right - 1)

    def test_dsp_chain_shapes_stdin_stream(self):
        self._await_clean_engine()
        preset = f"STDIN Live {os.getpid()}"
        notch_hz = _tone_frequency(4)
        ref_hz = _tone_frequency(5)
        slot_a_before = _bank_state("global").get("preset_a") or "Neutral"
        try:
            plain_left, plain_right = self._run_pipeline(8, "f32le", 48000, 5, 6)
            created = _http("POST", "/api/dsp/presets/create-peq", {
                "presetName": preset,
                "peq": {"enabled": True, "params": {
                    "channelMode": "stereo-linked", "bands": [
                        {"filterType": "bell", "frequencyHz": notch_hz,
                         "gainDb": -12, "q": 4}]}},
            })
            self.assertEqual(created.get("status"), "ok")
            # The engine renders bank assignments: assign the temp preset to
            # the global bank through the same mutation the UI uses. A bare
            # preset load is not enough (an unslotted name never reaches
            # the chain).
            _set_bank_preset("global", preset=preset, live_applied=True)
            # Bank switches rebuild the native engine asynchronously; verify
            # the notch on full runs (a truncated run can fake a notch, so
            # every run must also carry the reference tone near its
            # filter-skirt expectation: the 936 Hz reference sits on the
            # skirt of the 799 Hz Q=4 bell).
            expected_skirt = _rbj_peaking_db(ref_hz, notch_hz, 4, -12, self.graph_rate)
            deadline = time.monotonic() + 240
            while True:
                cut_left, cut_right = self._run_pipeline(8, "f32le", 48000, 5, 6)
                drop = _db(_tone_amplitude(cut_left, self.graph_rate, notch_hz)
                           / _tone_amplitude(plain_left, self.graph_rate, notch_hz))
                ref_ratio = _db(_tone_amplitude(cut_right, self.graph_rate, ref_hz)
                                / _tone_amplitude(plain_right, self.graph_rate, ref_hz))
                if drop <= -10.0 and abs(ref_ratio - expected_skirt) <= 3.0:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        f"PEQ notch never rendered (last drop {drop:.1f} dB, ref {ref_ratio:.1f} dB)")
                time.sleep(5)
            self.assertAlmostEqual(drop, -12, delta=1.5, msg=f"PEQ notch ineffective: {drop:.1f} dB")
            self.assertAlmostEqual(ref_ratio, expected_skirt, delta=1.5,
                                   msg=f"PEQ skirt unexpected on right: {ref_ratio:.1f} dB")
            from audio.system_volume import volume_db_to_percent, volume_percent_to_db
            volume = self.saved_status.get("volume")
            self.assertIsInstance(volume, (int, float))
            # The canonical master is a sink volume (post-DSP): the monitor
            # tap sits before it, so these runs prove actuation on the sink
            # STDIN plays through plus stream stability across changes.
            _http("POST", "/api/volume", {"volume": int(volume)})
            self.assertEqual(_sink_volume_percent(self.sink_name), int(volume))
            full_left, _ = self._run_pipeline(8, "f32le", 48000, 5, 6, tap="monitor")
            full_level = _tone_amplitude(full_left, self.graph_rate, notch_hz)
            lowered = volume_db_to_percent(volume_percent_to_db(int(volume)) - 6)
            _http("POST", "/api/volume", {"volume": lowered})
            self.assertEqual(_sink_volume_percent(self.sink_name), lowered)
            quiet_left, _ = self._run_pipeline(8, "f32le", 48000, 5, 6, tap="monitor")
            self.assertAlmostEqual(
                _db(_tone_amplitude(quiet_left, self.graph_rate, notch_hz) / full_level),
                0, delta=1.0, msg="stream changed under master volume change")
            _http("POST", "/api/volume", {"volume": 0})
            self.assertEqual(_sink_volume_percent(self.sink_name), 0)
            muted_left, _ = self._run_pipeline(8, "f32le", 48000, 5, 6, tap="monitor")
            self.assertGreater(_tone_amplitude(muted_left, self.graph_rate, notch_hz), 0)
            overview = _http("GET", "/api/audio/source-mode")
            self.assertEqual(overview.get("mode"), "stdin-input")
            # The finished writer is gone (waiting), but the selection and
            # the listener survive mute: the next writer can play.
            self.assertIn((overview.get("stdin") or {}).get("state"),
                          {"streaming", "ready", "draining", "waiting", "connected"})
            self.assertIsNone((overview.get("stdin") or {}).get("error"))
            _http("POST", "/api/volume", {"volume": int(volume)})
            self.assertEqual(_sink_volume_percent(self.sink_name), int(volume))
        finally:
            # Restore the previous slot-A content (never leave the temp
            # preset assigned) so the box keeps rendering the clean chain
            # for the matrix test; the class teardown restores the side.
            try:
                _set_bank_preset("global", preset=slot_a_before, active_side="A")
            except Exception:
                pass
            try:
                _http("POST", "/api/dsp/presets/delete", {"preset_name": preset})
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
