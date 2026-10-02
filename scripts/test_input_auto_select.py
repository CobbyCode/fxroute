#!/usr/bin/env python3
"""Input auto-select: signal analysis, hysteresis, switching and return."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.input_activity as activity
from audio.alsa_ctl import is_digital_lock_control, read_digital_lock
from audio.input_activity import ActivityDetector, BlockAnalysis, analyze_block, detect_encoding
from audio.input_auto_select import InputAutoSelect, InputAutoSelectDependencies, selection_key
from audio.samplerate.parsing import _parse_pactl_sources_detailed
from audio.source_feed import SourceOverviewFeed

RATE = 48_000
BLOCK = int(RATE * activity.BLOCK_SECONDS)


def sine_block(dbfs, frames=BLOCK):
    amplitude = 10 ** (dbfs / 20) * (2 ** 31 - 1)
    wave = numpy.sin(numpy.arange(frames) * 2 * numpy.pi * 997 / RATE) * amplitude
    return numpy.stack([wave, wave], axis=1).astype(numpy.int32)


def iec61937_block(data_type, frames=BLOCK, period=1536):
    """IEC 61937 bursts: Pa/Pb/Pc/Pd words then noise-like payload."""
    rng = numpy.random.default_rng(7)
    words = numpy.zeros((frames, 2), dtype=numpy.int64)
    for start in range(0, frames - 4, period):
        words[start] = (0xF872, 0x4E1F)
        words[start + 1] = (data_type, 0x3000)
        payload_end = min(start + 400, frames)
        if data_type not in (0, 3):
            words[start + 2:payload_end] = rng.integers(0, 0x10000, size=(payload_end - start - 2, 2))
    return ((words << 16) & 0xFFFFFFFF).astype(numpy.uint32).view(numpy.int32)


def analysis(peak, rms=None, *, silence=False, encoding=None, idle=False):
    return BlockAnalysis(peak, peak - 3 if rms is None else rms, silence, encoding, idle)


SILENT = analysis(float("-inf"), silence=True)


class SignalAnalysisTests(unittest.TestCase):
    def test_pcm_level_and_digital_silence(self):
        block = analyze_block(sine_block(-20.0))
        self.assertAlmostEqual(block.peak_dbfs, -20.0, delta=0.1)
        self.assertAlmostEqual(block.rms_dbfs, -23.0, delta=0.2)
        self.assertFalse(block.digital_silence)
        self.assertIsNone(block.encoding)
        silent = analyze_block(numpy.zeros((BLOCK, 2), dtype=numpy.int32))
        self.assertTrue(silent.digital_silence)
        self.assertEqual(silent.peak_dbfs, float("-inf"))

    def test_iec61937_codecs_and_idle_bursts(self):
        self.assertEqual(detect_encoding(iec61937_block(1)), ("ac3", False))
        self.assertEqual(detect_encoding(iec61937_block(11, period=512)), ("dts", False))
        self.assertEqual(detect_encoding(iec61937_block(21, period=1024)), ("eac3", False))
        self.assertEqual(detect_encoding(iec61937_block(3)), (None, True), "pause bursts are no programme")
        self.assertEqual(detect_encoding(iec61937_block(0)), (None, True), "null bursts are no programme")

    def test_raw_dts_core_sync_in_pcm(self):
        words = numpy.zeros((BLOCK, 2), dtype=numpy.int64)
        words[100] = (0x1FFF, 0xE800)
        words[101] = (0x07F1, 0)
        frames = ((words << 16) & 0xFFFFFFFF).astype(numpy.uint32).view(numpy.int32)
        self.assertEqual(detect_encoding(frames), ("dts", False))

    def test_music_noise_is_plain_pcm(self):
        rng = numpy.random.default_rng(3)
        frames = rng.integers(-2 ** 28, 2 ** 28, size=(BLOCK, 2)).astype(numpy.int32)
        self.assertEqual(detect_encoding(frames), (None, False))


class DetectorTests(unittest.TestCase):
    def feed(self, detector, start, seconds, block):
        now = start
        steps = int(round(seconds / activity.BLOCK_SECONDS))
        for _ in range(steps):
            now += activity.BLOCK_SECONDS
            detector.observe_block(now, block)
        return now

    def test_analog_attack_needs_sustained_programme(self):
        detector = ActivityDetector(activity.KIND_ANALOG, 0.0)
        now = self.feed(detector, 0.0, 0.8, analysis(-20.0))
        self.assertFalse(detector.active, "0.8 s is below the attack time")
        self.feed(detector, now, 0.4, analysis(-20.0))
        self.assertTrue(detector.active)
        self.assertEqual(detector.format, "pcm")

    def test_clicks_and_noise_floor_never_arm(self):
        detector = ActivityDetector(activity.KIND_ANALOG, 0.0)
        now = 0.0
        for _ in range(20):
            now = self.feed(detector, now, 0.1, analysis(-10.0))
            now = self.feed(detector, now, 0.5, analysis(-95.0))
        self.assertFalse(detector.active, "isolated clicks restart the attack")
        self.feed(detector, now, 30, analysis(-62.0, -70.0))
        self.assertFalse(detector.active, "hiss/hum below the arm level")

    def test_pauses_and_quiet_passages_do_not_release(self):
        detector = ActivityDetector(activity.KIND_ANALOG, 0.0)
        now = self.feed(detector, 0.0, 2, analysis(-20.0))
        now = self.feed(detector, now, 4, analysis(-100.0))   # track change
        now = self.feed(detector, now, 120, analysis(-55.0, -70.0))  # quiet passage
        now = self.feed(detector, now, 59, analysis(-100.0))
        self.assertTrue(detector.active, "59 s of silence is still a pause")
        self.feed(detector, now, 1.1, analysis(-100.0))
        self.assertFalse(detector.active, "released after the 60 s hold")

    def test_digital_silence_switches_to_digital_thresholds(self):
        detector = ActivityDetector(activity.KIND_ANALOG, 0.0)
        now = self.feed(detector, 0.0, 1, SILENT)
        now = self.feed(detector, now, 5, analysis(-90.0))
        self.assertFalse(detector.active, "dither level is no programme")
        self.feed(detector, now, 0.7, analysis(-70.0, -80.0))
        self.assertTrue(detector.active, "digital PCM arms above dither after 0.5 s")

    def test_lost_lock_releases_after_short_hold(self):
        detector = ActivityDetector(activity.KIND_DIGITAL, 0.0)
        now = self.feed(detector, 0.0, 1, analysis(-30.0))
        self.assertTrue(detector.active)
        for _ in range(95):
            now += activity.BLOCK_SECONDS
            detector.observe_block(now, SILENT, lock=False)
        self.assertTrue(detector.active)
        for _ in range(6):
            now += activity.BLOCK_SECONDS
            detector.observe_block(now, SILENT, lock=False)
        self.assertFalse(detector.active, "unlocked receiver releases after 10 s")

    def test_locked_digital_silence_waits_the_full_hold(self):
        detector = ActivityDetector(activity.KIND_DIGITAL, 0.0)
        now = self.feed(detector, 0.0, 1, analysis(-30.0))
        for _ in range(300):
            now += activity.BLOCK_SECONDS
            detector.observe_block(now, SILENT, lock=True)
        self.assertTrue(detector.active, "a paused player keeps the lock")

    def test_encoded_stream_needs_confirmation(self):
        detector = ActivityDetector(activity.KIND_DIGITAL, 0.0)
        now = self.feed(detector, 0.0, 0.1, analysis(-6.0, encoding="ac3"))
        now = self.feed(detector, now, 0.1, analysis(-6.0))
        self.assertNotEqual(detector._stream, "ac3", "one chance sync match never flips the stream")
        self.feed(detector, now, 1, analysis(-6.0, encoding="ac3"))
        self.assertTrue(detector.active)
        self.assertEqual(detector.format, "ac3")

    def test_pause_bursts_release_like_silence(self):
        detector = ActivityDetector(activity.KIND_DIGITAL, 0.0)
        now = self.feed(detector, 0.0, 1, analysis(-6.0, encoding="dts"))
        self.assertTrue(detector.active)
        self.feed(detector, now, 61, analysis(-6.0, idle=True))
        self.assertFalse(detector.active, "encoded pause carries no programme")

    def test_missing_input_releases_after_short_hold(self):
        detector = ActivityDetector(activity.KIND_ANALOG, 0.0)
        now = self.feed(detector, 0.0, 2, analysis(-20.0))
        detector.observe_missing(now + 9)
        self.assertTrue(detector.active)
        detector.observe_missing(now + 10.5)
        self.assertFalse(detector.active)

    def test_stdin_presence(self):
        detector = ActivityDetector(activity.KIND_STDIN, 0.0)
        detector.observe_presence(0.5, True)
        self.assertTrue(detector.active, "a connected writer is an explicit signal")
        detector.observe_presence(5.0, False)
        self.assertTrue(detector.active)
        detector.observe_presence(10.6, False)
        self.assertFalse(detector.active)

    def test_assumed_active_keeps_a_fresh_hold(self):
        detector = ActivityDetector(activity.KIND_ANALOG, 100.0, assume_active=True)
        detector.update(159.0)
        self.assertTrue(detector.active)
        detector.update(160.0)
        self.assertFalse(detector.active)


class AlsaLockTests(unittest.TestCase):
    def test_lock_control_names(self):
        for name in ("S/PDIF Validity", "IEC958 In Lock", "SPDIF In Status", "Optical Sync Status"):
            self.assertTrue(is_digital_lock_control(name), name)
        for name in ("IEC958 Playback Switch", "Internal Validity", "ADAT Validity",
                     "Clock Source Clock Source", "SPDIF Out Status", "Capture Switch"):
            self.assertFalse(is_digital_lock_control(name), name)

    def test_lock_state_from_controls(self):
        with patch("audio.alsa_ctl.read_boolean_controls",
                   return_value={"S/PDIF Validity": False, "Internal Validity": True}):
            self.assertIs(read_digital_lock(3), False)
        with patch("audio.alsa_ctl.read_boolean_controls", return_value={"Capture Switch": True}):
            self.assertIsNone(read_digital_lock(3), "no lock control: unknown")
        with patch("audio.alsa_ctl.read_boolean_controls", side_effect=OSError("denied")):
            self.assertIsNone(read_digital_lock(3))
        self.assertIsNone(read_digital_lock(None))

    def test_source_details_carry_the_alsa_card(self):
        details = _parse_pactl_sources_detailed(
            "Source #12\n\tName: alsa_input.usb-X-00.iec958-stereo\n\tProperties:\n"
            "\t\talsa.card = \"2\"\n\t\tapi.alsa.pcm.card = \"2\"\n")
        self.assertEqual(details["alsa_input.usb-X-00.iec958-stereo"]["alsa_card"], 2)


class FeedDecorationTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_record_and_push_carries_the_decoration(self):
        pushes = []

        async def broadcast(message):
            pushes.append(message)

        state = {"armed": []}
        feed = SourceOverviewFeed(broadcast, decorate=lambda overview: {**overview, "auto_select": dict(state)})
        recorded = feed.record({"mode": "app-playback"})
        self.assertEqual(recorded["auto_select"], {"armed": []})
        state["armed"] = ["x"]
        await feed.publish(feed.latest, full_build=False)
        self.assertEqual(pushes[-1]["data"]["auto_select"], {"armed": ["x"]})


X = "alsa_input.usb-Focusrite-00.pro-input-0|pair:AUX0,AUX1"
Y = "alsa_input.usb-SPDIF-00.iec958-stereo"
Z = "alsa_input.pci-0000.analog-stereo"


def _entry(key, source, left="FL", right="FR", **extra):
    return {"key": key, "source_key": source, "name": source, "left_channel": left,
            "right_channel": right, "is_active_port": True, "alsa_card": 3, **extra}


class FakeProbe:
    def __init__(self, spec, detector):
        self.spec = spec
        self.detector = detector
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class Harness:
    """Controller with an in-memory selection, overview and probes."""

    def __init__(self, test, state_dir, *, mode="app-playback", input_key=None):
        self.now = 1000.0
        self.selection = {"mode": mode, "selected_input_key": input_key}
        self.inputs = [
            _entry(X, "alsa_input.usb-Focusrite-00.pro-input-0", "AUX0", "AUX1"),
            _entry(Y, "alsa_input.usb-SPDIF-00.iec958-stereo"),
            _entry(Z, "alsa_input.pci-0000.analog-stereo"),
        ]
        self.unavailable = set()
        self.switches = []
        self.publishes = 0
        self.measurement = False
        self.stdin = None
        self.probes = {}
        self.state_path = Path(state_dir) / "input-auto-select.json"

        async def read_overview():
            return {"inputs": self.inputs}

        async def publish_state():
            self.publishes += 1
            return {"auto_select": self.controller.snapshot()}

        async def switch_if(decide):
            target = decide(dict(self.selection))
            if target is None:
                return None
            mode, key = target
            if (key or mode) in self.unavailable:
                raise ValueError(f"{key or mode} is not currently available")
            self.selection = {"mode": mode, "selected_input_key": key if key else self.selection["selected_input_key"]}
            self.switches.append(target)
            return {"mode": mode}

        def probe_factory(spec, detector):
            probe = FakeProbe(spec, detector)
            self.probes[spec.source_name] = probe
            return probe

        self.controller = InputAutoSelect(InputAutoSelectDependencies(
            get_fresh_overview=lambda max_age: {"inputs": self.inputs},
            read_overview=read_overview,
            publish_state=publish_state,
            load_selection=lambda: dict(self.selection),
            switch_if=switch_if,
            get_stdin_status=lambda: self.stdin,
            measurement_owns_graph=lambda: self.measurement,
            resolve_capture_rate=lambda: RATE,
            probe_factory=probe_factory,
            state_path=lambda: self.state_path,
            monotonic=lambda: self.now,
        ))

    def detector(self, key):
        return self.controller._detectors[key]

    async def tick(self, seconds=0.5):
        self.now += seconds
        await self.controller._tick()

    async def signal(self, key, seconds, dbfs=-20.0):
        """Feed programme blocks to ``key``'s detector for ``seconds``, ticking."""
        for _ in range(int(round(seconds / 0.5))):
            for _ in range(5):
                self.now += 0.1
                self.detector(key).observe_block(self.now, analysis(dbfs))
            await self.controller._tick()

    async def silence(self, key, seconds):
        for _ in range(int(round(seconds / 0.5))):
            for _ in range(5):
                self.now += 0.1
                self.detector(key).observe_block(self.now, analysis(-110.0))
            await self.controller._tick()


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def harness(self, **kwargs):
        return Harness(self, self.tmp.name, **kwargs)

    async def test_signal_switches_and_silence_returns(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        await h.tick()
        self.assertTrue(h.probes["alsa_input.usb-Focusrite-00.pro-input-0"].started)
        self.assertEqual(h.probes["alsa_input.usb-Focusrite-00.pro-input-0"].spec.left_channel, "AUX0")
        await h.signal(X, 2)
        self.assertEqual(h.switches, [("external-input", X)])
        self.assertEqual(h.controller.snapshot()["engaged"],
                         {"key": X, "return_mode": "app-playback", "return_input_key": None})
        await h.silence(X, 30)
        await h.signal(X, 1)
        await h.silence(X, 59)
        self.assertEqual(len(h.switches), 1, "pauses never return")
        await h.silence(X, 2)
        self.assertEqual(h.switches[-1], ("app-playback", None))
        self.assertIsNone(h.controller.snapshot()["engaged"])

    async def test_manual_selection_becomes_the_return_target(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        await h.tick()
        await h.signal(X, 2)
        # Manual choice while engaged ends the engagement: no return.
        h.selection = {"mode": "external-input", "selected_input_key": Z}
        await h.signal(X, 1)
        self.assertIsNone(h.controller.snapshot()["engaged"])
        await h.silence(X, 62)
        self.assertEqual(h.switches, [("external-input", X)])
        # Next signal returns to the manual selection.
        await h.signal(X, 2)
        self.assertEqual(h.switches[-1], ("external-input", X))
        self.assertEqual(h.controller.snapshot()["engaged"]["return_input_key"], Z)
        await h.silence(X, 61)
        self.assertEqual(h.switches[-1], ("external-input", Z))

    async def test_signal_on_the_selected_input_does_not_engage(self):
        h = self.harness(mode="external-input", input_key=X)
        await h.controller.set_armed(X, True)
        await h.tick()
        await h.signal(X, 2)
        await h.silence(X, 62)
        self.assertEqual(h.switches, [])
        self.assertIsNone(h.controller.snapshot()["engaged"])

    async def test_manual_app_selection_rearms_after_short_silence(self):
        h = self.harness(mode="external-input", input_key=X)
        await h.controller.set_armed(X, True)
        await h.tick()
        await h.signal(X, 2)
        self.assertEqual(h.switches, [], "arming the selected input does not engage it")
        h.selection = {"mode": "app-playback", "selected_input_key": X}
        await h.signal(X, 3)
        await h.silence(X, 0.5)
        await h.signal(X, 2)
        self.assertEqual(h.switches, [], "ongoing signal and brief gaps respect the manual choice")
        await h.silence(X, 2)
        self.assertFalse(h.controller.snapshot()["inputs"][X]["active"])
        await h.signal(X, 0.5)
        self.assertEqual(h.switches, [], "a new signal still needs the normal attack")
        await h.signal(X, 1.5)
        self.assertEqual(h.switches, [("external-input", X)])
        self.assertEqual(h.controller.snapshot()["engaged"]["return_mode"], "app-playback")
        await h.silence(X, 59)
        self.assertEqual(h.switches, [("external-input", X)], "automatic return keeps the long hold")
        await h.silence(X, 2)
        self.assertEqual(h.switches[-1], ("app-playback", None))

    async def test_manual_override_rearms_an_automatically_selected_input(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        await h.tick()
        await h.signal(X, 2)
        h.selection = {"mode": "external-input", "selected_input_key": Z}
        await h.signal(X, 2)
        self.assertIsNone(h.controller.snapshot()["engaged"])
        self.assertEqual(h.switches, [("external-input", X)], "ongoing signal does not undo the override")
        await h.silence(X, 2)
        await h.signal(X, 2)
        self.assertEqual(h.switches, [("external-input", X), ("external-input", X)])
        self.assertEqual(h.controller.snapshot()["engaged"]["return_input_key"], Z)

    async def test_digital_input_rearms_after_manual_app_selection(self):
        h = self.harness(mode="external-input", input_key=Y)
        await h.controller.set_armed(Y, True)
        await h.tick()
        await h.signal(Y, 2)
        h.selection = {"mode": "app-playback", "selected_input_key": Y}
        await h.signal(Y, 2)
        self.assertEqual(h.switches, [])
        await h.silence(Y, 2)
        await h.signal(Y, 1.5)
        self.assertEqual(h.switches, [("external-input", Y)])

    async def test_silent_input_at_arming_switches_on_the_first_signal(self):
        h = self.harness(mode="external-input", input_key=X)
        await h.controller.set_armed(X, True)
        await h.tick()
        await h.silence(X, 2)
        h.selection = {"mode": "app-playback", "selected_input_key": X}
        await h.signal(X, 2)
        self.assertEqual(h.switches, [("external-input", X)])

    async def test_first_signal_keeps_the_route_then_hands_over(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        await h.controller.set_armed(Y, True)
        await h.tick()
        self.assertTrue(h.detector(Y)._digital, "iec958 source uses digital detection")
        await h.signal(X, 2)
        for _ in range(4):
            await h.signal(Y, 0.5)
            await h.signal(X, 0.5)
        self.assertEqual(h.switches, [("external-input", X)], "an active engaged input is never preempted")
        for _ in range(125):
            await h.signal(Y, 0.5)
        self.assertEqual(h.switches[-1], ("external-input", Y), "still-active input takes over")
        self.assertEqual(h.controller.snapshot()["engaged"]["return_mode"], "app-playback")
        await h.silence(Y, 61)
        self.assertEqual(h.switches[-1], ("app-playback", None))

    async def test_unavailable_return_target_falls_back_to_app_playback(self):
        h = self.harness(mode="external-input", input_key=Z)
        await h.controller.set_armed(X, True)
        await h.tick()
        await h.signal(X, 2)
        h.unavailable.add(Z)
        await h.silence(X, 61)
        self.assertEqual(h.switches[-1], ("app-playback", None))
        self.assertIsNone(h.controller.snapshot()["engaged"])

    async def test_measurement_pauses_probes_and_switches(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        await h.tick()
        probe = h.probes["alsa_input.usb-Focusrite-00.pro-input-0"]
        h.measurement = True
        await h.tick()
        self.assertTrue(probe.stopped)
        self.assertTrue(h.controller.snapshot()["paused"])
        h.detector(X).active = True
        await h.tick()
        self.assertEqual(h.switches, [], "no switch while measurement owns the graph")
        h.measurement = False
        await h.tick()
        self.assertFalse(h.controller.snapshot()["paused"])

    async def test_missing_input_stops_its_probe_and_releases(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        await h.tick()
        await h.signal(X, 2)
        h.inputs = [entry for entry in h.inputs if entry["key"] != X]
        await h.tick()
        self.assertTrue(h.probes["alsa_input.usb-Focusrite-00.pro-input-0"].stopped)
        for _ in range(22):
            await h.tick()
        self.assertEqual(h.switches[-1], ("app-playback", None))

    async def test_stdin_writer_switches_and_returns(self):
        h = self.harness()
        await h.controller.set_armed("stdin-input", True)
        h.stdin = {"session_id": "abc", "state": "connected"}
        await h.tick()
        self.assertEqual(h.switches, [("stdin-input", None)])
        h.stdin = {"session_id": None, "state": "waiting"}
        for _ in range(19):
            await h.tick()
        self.assertEqual(len(h.switches), 1, "a short gap between writers keeps STDIN")
        await h.tick(1.0)
        self.assertEqual(h.switches[-1], ("app-playback", None))

    async def test_stdin_reconnect_rearms_after_manual_app_selection(self):
        h = self.harness(mode="stdin-input")
        await h.controller.set_armed("stdin-input", True)
        h.stdin = {"session_id": "abc", "state": "connected"}
        await h.tick()
        h.selection = {"mode": "app-playback", "selected_input_key": None}
        await h.tick()
        self.assertEqual(h.switches, [], "the existing writer does not undo the manual choice")
        h.stdin = {"session_id": None, "state": "waiting"}
        await h.tick(2.0)
        h.stdin = {"session_id": "def", "state": "connected"}
        await h.tick()
        self.assertEqual(h.switches, [("stdin-input", None)])

    async def test_disarming_the_engaged_input_stays_on_it(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        await h.tick()
        await h.signal(X, 2)
        await h.controller.set_armed(X, False)
        await h.tick()
        self.assertIsNone(h.controller.snapshot()["engaged"])
        self.assertTrue(h.probes["alsa_input.usb-Focusrite-00.pro-input-0"].stopped)
        self.assertEqual(h.switches, [("external-input", X)])

    async def test_rapid_auto_off_on_rearms_the_reused_detector(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        await h.tick()
        await h.signal(X, 2)
        await h.controller.set_armed(X, False)
        await h.controller.set_armed(X, True)
        h.selection = {"mode": "app-playback", "selected_input_key": X}
        await h.signal(X, 2)
        self.assertEqual(h.switches, [("external-input", X)], "ongoing signal respects the manual choice")
        await h.silence(X, 2)
        self.assertFalse(h.controller.snapshot()["inputs"][X]["active"])
        await h.signal(X, 2)
        self.assertEqual(h.switches, [("external-input", X), ("external-input", X)])
        self.assertEqual(h.controller.snapshot()["engaged"]["return_mode"], "app-playback")

    async def test_unknown_key_is_rejected(self):
        h = self.harness()
        with self.assertRaises(ValueError):
            await h.controller.set_armed("alsa_input.gone", True)
        with self.assertRaises(ValueError):
            await h.controller.set_armed("", True)

    async def test_engagement_survives_a_restart(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        await h.tick()
        await h.signal(X, 2)
        saved = json.loads(h.state_path.read_text())
        self.assertEqual(saved["armed"], [X])
        self.assertEqual(saved["engaged"]["key"], X)
        restarted = Harness(self, self.tmp.name, mode="external-input", input_key=X)
        await restarted.tick()
        self.assertEqual(restarted.switches, [], "no immediate return after a restart")
        self.assertTrue(restarted.detector(X).active)
        for _ in range(122):
            await restarted.tick()
        self.assertEqual(restarted.switches[-1], ("app-playback", None), "returns once the hold passed")

    async def test_failed_switch_restores_the_engagement(self):
        h = self.harness()
        h.unavailable.add(X)
        await h.controller.set_armed(X, True)
        await h.tick()
        await h.signal(X, 2)
        self.assertEqual(h.switches, [])
        self.assertIsNone(h.controller.snapshot()["engaged"])

    async def test_state_changes_are_published(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        before = h.publishes
        await h.tick()
        await h.signal(X, 2)
        self.assertGreater(h.publishes, before)
        quiet = h.publishes
        await h.signal(X, 2)
        self.assertEqual(h.publishes, quiet, "unchanged state is not republished")

    def test_selection_key(self):
        self.assertEqual(selection_key({"mode": "external-input", "selected_input_key": X}), X)
        self.assertEqual(selection_key({"mode": "app-playback", "selected_input_key": X}), "app-playback")
        self.assertEqual(selection_key({}), "app-playback")


if __name__ == "__main__":
    unittest.main()
