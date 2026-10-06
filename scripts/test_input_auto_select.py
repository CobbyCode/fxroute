#!/usr/bin/env python3
"""Input auto-select: signal analysis, hysteresis, switching and return.

Includes the external presence feed OR-ed into the detector and the
unchanged analog detection next to it.
"""

import asyncio
import ctypes
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.alsa_ctl as alsa_ctl
import audio.input_activity as activity
from audio.alsa_ctl import clock_source_target, is_digital_lock_control, read_digital_lock, select_clock_source
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

    def test_external_presence_arms_immediately_and_holds_the_release(self):
        detector = ActivityDetector(activity.KIND_ANALOG, 0.0)
        detector.observe_external_presence(1.0, True)
        self.assertTrue(detector.active, "an external report arms without the analog attack")
        self.assertEqual(detector.format, "pcm")
        detector.observe_external_presence(30.0, False)
        self.assertTrue(detector.active, "the hold still applies once the report ends")
        detector.update(60.9)
        self.assertTrue(detector.active, "less than the 60 s hold")
        detector.update(61.5)
        self.assertFalse(detector.active, "released after the hold")

    def test_external_presence_ors_with_local_detection(self):
        detector = ActivityDetector(activity.KIND_ANALOG, 0.0)
        detector.observe_external_presence(0.0, True)
        now = self.feed(detector, 0.0, 10, analysis(-100.0))
        self.assertTrue(detector.active, "local silence cannot release a fresh report")
        detector.observe_external_presence(now, False)
        now = self.feed(detector, now, 5, analysis(-20.0))
        self.assertTrue(detector.active, "an ongoing local signal keeps it active")
        now = self.feed(detector, now, 59, analysis(-100.0))
        self.assertTrue(detector.active, "the analog hold still applies")
        self.feed(detector, now, 2, analysis(-100.0))
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

    def test_clock_follows_only_a_locked_digital_source(self):
        names = ["Internal", "S/PDIF", "ADAT"]
        locked = {"Internal Validity": True, "S/PDIF Validity": True, "ADAT Validity": True}
        unlocked = {**locked, "S/PDIF Validity": False}
        self.assertEqual(clock_source_target(names, 0, locked, digital=True), 1)
        self.assertIsNone(clock_source_target(names, 0, unlocked, digital=True), "no lock: stay internal")
        self.assertIsNone(clock_source_target(names, 1, locked, digital=True))
        self.assertIsNone(clock_source_target(names, 2, locked, digital=True), "ADAT is the user's choice")
        self.assertIsNone(clock_source_target(["Clock 1", "S/PDIF"], 0, locked, digital=True))

    def test_clock_release_returns_only_digital_to_internal(self):
        names = ["Internal", "S/PDIF", "ADAT"]
        self.assertEqual(clock_source_target(names, 1, {}, digital=False), 0)
        self.assertIsNone(clock_source_target(names, 0, {}, digital=False))
        self.assertIsNone(clock_source_target(names, 2, {}, digital=False))
        self.assertIsNone(clock_source_target(names, 7, {}, digital=False))

    def test_clock_selection_without_a_card_is_a_no_op(self):
        self.assertIsNone(select_clock_source(None, digital=True))
        with patch("audio.alsa_ctl.os.open", side_effect=FileNotFoundError("gone")):
            self.assertIsNone(select_clock_source(3, digital=False))

    def fake_card(self, *, current, receiver=True):
        """Control layer of one card: a clock selector and, optionally, a receiver lock."""
        names = ["Internal", "S/PDIF", "ADAT"]
        state = {"clock": current, "writes": [], "locked": True}
        elements = []
        selector = alsa_ctl._ElemId(numid=4, name=b"Clock Source Clock Source")
        selector_info = alsa_ctl._ElemInfo(id=selector, type=3, access=3, count=1)
        selector_info.value.enumerated.items = len(names)
        elements.append((selector, selector_info))
        for numid, name in ((1, b"Internal Validity"), (2, b"S/PDIF Validity")):
            if name == b"S/PDIF Validity" and not receiver:
                continue
            valid = alsa_ctl._ElemId(numid=numid, name=name)
            elements.append((valid, alsa_ctl._ElemInfo(id=valid, type=1, access=1, count=1)))

        def ioctl(_fd, request, arg, _mutate):
            if request == alsa_ctl._IOCTL_ELEM_INFO:
                arg.value.enumerated.name = names[arg.value.enumerated.item].encode()
            elif request == alsa_ctl._IOCTL_ELEM_READ:
                if arg.id.name == b"Clock Source Clock Source":
                    arg.value.enumerated[0] = state["clock"]
                else:
                    arg.value.integer[0] = int(state["locked"] or arg.id.name != b"S/PDIF Validity")
            elif request == alsa_ctl._IOCTL_ELEM_WRITE:
                state["clock"] = arg.value.enumerated[0]
                state["writes"].append(names[state["clock"]])

        patches = (patch("audio.alsa_ctl.os.open", return_value=99), patch("audio.alsa_ctl.os.close"),
                   patch("audio.alsa_ctl._readable_elements", return_value=elements),
                   patch("audio.alsa_ctl.fcntl.ioctl", ioctl))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return state

    def test_stale_clock_release_needs_a_digital_receiver(self):
        state = self.fake_card(current=1, receiver=False)
        self.assertIsNone(select_clock_source(3, digital=False, require_receiver=True))
        self.assertEqual(state["writes"], [], "a card without a digital receiver keeps its clock")
        self.assertEqual(select_clock_source(3, digital=False), "Internal", "the owner's release is unchanged")

    def test_stale_clock_release_moves_a_digital_clock_to_internal_only(self):
        state = self.fake_card(current=1)
        self.assertEqual(select_clock_source(3, digital=False, require_receiver=True), "Internal")
        self.assertEqual(state["writes"], ["Internal"])
        self.assertIsNone(select_clock_source(3, digital=False, require_receiver=True))
        state["clock"] = 2
        self.assertIsNone(select_clock_source(3, digital=False, require_receiver=True), "ADAT stays")
        self.assertEqual(state["writes"], ["Internal"])

    def test_cards_are_listed_from_the_control_nodes(self):
        nodes = [Path("/dev/snd/controlC10"), Path("/dev/snd/controlC3"), Path("/dev/snd/timer"),
                 Path("/dev/snd/controlCx")]
        with patch.object(alsa_ctl.Path, "glob", return_value=nodes):
            self.assertEqual(alsa_ctl.list_cards(), [3, 10])
        with patch.object(alsa_ctl.Path, "glob", side_effect=OSError("denied")):
            self.assertEqual(alsa_ctl.list_cards(), [])

    def test_settle_returns_a_stale_digital_clock_to_internal(self):
        state = self.fake_card(current=1)
        with patch.object(alsa_ctl, "list_cards", return_value=[3]):
            self.assertEqual(alsa_ctl.settle_digital_clocks(lambda card: False), [(3, False, "Internal")])
            self.assertEqual(alsa_ctl.settle_digital_clocks(lambda card: False), [])
        self.assertEqual(state["writes"], ["Internal"])

    def test_settle_restores_the_digital_clock_the_input_holds(self):
        """The device came back on Internal: the holder's card follows the receiver again."""
        state = self.fake_card(current=0)
        with patch.object(alsa_ctl, "list_cards", return_value=[3]):
            self.assertEqual(alsa_ctl.settle_digital_clocks(lambda card: True), [(3, True, "S/PDIF")])
            self.assertEqual(alsa_ctl.settle_digital_clocks(lambda card: True), [])
        self.assertEqual(state["writes"], ["S/PDIF"])

    def test_settle_waits_for_the_receiver_lock(self):
        state = self.fake_card(current=0)
        state["locked"] = False
        with patch.object(alsa_ctl, "list_cards", return_value=[3]):
            self.assertEqual(alsa_ctl.settle_digital_clocks(lambda card: True), [])
            self.assertEqual(state["writes"], [], "no lock: stay on Internal")
            state["locked"] = True
            self.assertEqual(alsa_ctl.settle_digital_clocks(lambda card: True), [(3, True, "S/PDIF")])

    def test_settle_leaves_adat_and_unmanaged_cards_alone(self):
        state = self.fake_card(current=2)
        with patch.object(alsa_ctl, "list_cards", return_value=[3]):
            for wanted in (True, False, None):
                self.assertEqual(alsa_ctl.settle_digital_clocks(lambda card: wanted), [])
        state["clock"] = 1
        with patch.object(alsa_ctl, "list_cards", return_value=[3]):
            self.assertEqual(alsa_ctl.settle_digital_clocks(lambda card: None), [])
        self.assertEqual(state["writes"], [], "ADAT and an unmanaged card keep their clock")

    def test_settle_skips_a_card_without_a_digital_receiver(self):
        state = self.fake_card(current=1, receiver=False)
        with patch.object(alsa_ctl, "list_cards", return_value=[3]):
            self.assertEqual(alsa_ctl.settle_digital_clocks(lambda card: False), [])
        self.assertEqual(state["writes"], [])

    def test_settle_gives_each_card_its_own_clock(self):
        calls = []

        def select(card, *, digital, require_receiver):
            calls.append((card, digital, require_receiver))
            return "src"

        with patch.object(alsa_ctl, "list_cards", return_value=[2, 3, 4]), \
                patch.object(alsa_ctl, "_select_clock_source", select):
            settled = alsa_ctl.settle_digital_clocks(lambda card: {2: False, 3: True}.get(card))
        self.assertEqual(calls, [(2, False, True), (3, True, True)])
        self.assertEqual(settled, [(2, False, "src"), (3, True, "src")])

    def test_settle_asks_for_the_wish_under_the_clock_lock(self):
        """The digital input claims the clock while a settle waits for the lock."""
        claimed = threading.Event()
        calls = []

        def select(card, *, digital, require_receiver):
            calls.append((card, digital))
            return "src"

        with patch.object(alsa_ctl, "list_cards", return_value=[3]), \
                patch.object(alsa_ctl, "_select_clock_source", select):
            result = []
            with alsa_ctl._CLOCK_LOCK:
                worker = threading.Thread(target=lambda: result.append(
                    alsa_ctl.settle_digital_clocks(lambda card: claimed.is_set())))
                worker.start()
                time.sleep(0.05)
                claimed.set()
            worker.join(2)
        self.assertEqual(calls, [(3, True)], "the claim made while waiting decides, not the earlier state")
        self.assertEqual(result, [[(3, True, "src")]])

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_long) == 8, "LP64 kernel ABI sizes")
    def test_control_structs_match_the_kernel_abi(self):
        self.assertEqual(ctypes.sizeof(alsa_ctl._ElemInfo), 272)
        self.assertEqual(ctypes.sizeof(alsa_ctl._ElemValue), 1224)

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
        self.presence = {}
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
            external_presence=lambda key: self.presence.get(key, False),
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

    async def test_external_presence_switches_and_returns(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        await h.tick()
        self.assertEqual(h.switches, [], "no presence yet")
        h.presence[X] = True
        await h.tick()
        self.assertEqual(h.switches, [("external-input", X)])
        self.assertEqual(h.controller.snapshot()["engaged"]["key"], X)
        self.assertTrue(h.controller.snapshot()["inputs"][X]["presence"])
        h.presence[X] = False
        await h.tick()
        self.assertEqual(len(h.switches), 1, "presence off keeps the release hold")
        await h.tick(61)
        self.assertEqual(h.switches[-1], ("app-playback", None))
        self.assertIsNone(h.controller.snapshot()["engaged"])

    async def test_external_presence_composes_with_the_local_signal(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        h.presence[X] = True
        await h.tick()
        self.assertEqual(h.switches, [("external-input", X)])
        h.presence[X] = False
        await h.signal(X, 2)
        self.assertTrue(h.detector(X).active, "the local signal keeps the input active")
        self.assertEqual(len(h.switches), 1)
        await h.silence(X, 61)
        self.assertEqual(h.switches[-1], ("app-playback", None))

    async def test_auto_off_ends_a_presence_engagement_without_returning(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        h.presence[X] = True
        await h.tick()
        self.assertEqual(h.switches, [("external-input", X)])
        await h.controller.set_armed(X, False)
        await h.tick()
        self.assertEqual(h.controller.snapshot()["inputs"], {})
        self.assertIsNone(h.controller.snapshot()["engaged"])
        await h.tick(3)
        self.assertEqual(h.switches, [("external-input", X)], "unarmed presence never switches")

    async def test_analog_attack_is_unchanged_with_a_presence_reader(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        await h.tick()
        await h.signal(X, 0.8)
        self.assertEqual(h.switches, [], "0.8 s stays below the analog attack")
        await h.signal(X, 0.4)
        self.assertEqual(h.switches, [("external-input", X)])

    async def test_presence_reader_failure_is_fail_closed(self):
        h = self.harness()
        await h.controller.set_armed(X, True)
        await h.tick()
        h.presence[X] = True

        def boom(key):
            raise RuntimeError("feed down")

        h.controller._deps.external_presence = boom
        await h.tick()
        self.assertEqual(h.switches, [], "a failing reader never counts as presence")
        self.assertFalse(h.controller.snapshot()["inputs"][X]["presence"])

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
