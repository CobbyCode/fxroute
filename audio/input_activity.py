# SPDX-License-Identifier: AGPL-3.0-only
"""Signal activity detection for input auto-select.

Pure analysis and the per-input state machine; the capture probe feeds it
blocks of interleaved 32-bit stereo PCM, STDIN feeds writer presence.

Detection works on the bit-exact stream:

* digital silence: an idle digital receiver (no lock, paused player)
  delivers exact zeros, an analog converter never does; a pair that once
  delivered an all-zero block is treated as digital from then on.
* encoded bitstreams: IEC 61937 bursts (AC-3, E-AC-3, DTS, AAC, MPEG) are
  recognized by their Pa/Pb sync words, raw DTS (DTS-CD/LD) by its core
  sync; null and pause bursts count as silence.
* PCM level: analog arms on a sustained programme level and stays active
  while peaks stay above a much lower floor; digital PCM is active above
  dither level.

Hysteresis: arming needs a sustained signal (attack), releasing needs a
continuous absence for the release hold, so pauses, track changes and
quiet passages never release. A lost digital lock or a missing input
releases after a shorter hold, since that is a definite end of signal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy

BLOCK_SECONDS = 0.1

ANALOG_ON_RMS_DBFS = -50.0
ANALOG_KEEP_PEAK_DBFS = -60.0
DIGITAL_ON_PEAK_DBFS = -80.0

ANALOG_ATTACK_SECONDS = 1.0
DIGITAL_ATTACK_SECONDS = 0.5
# A gap up to this long inside the attack window does not restart it.
ATTACK_GAP_SECONDS = 0.3
RELEASE_SECONDS = 60.0
UNLOCKED_RELEASE_SECONDS = 10.0
MISSING_RELEASE_SECONDS = 10.0
STDIN_RELEASE_SECONDS = 10.0
# Consecutive blocks before the detected stream type (PCM or a codec)
# changes, so a chance sync-word match inside PCM never flips it.
STREAM_CONFIRM_BLOCKS = 2

KIND_ANALOG = "analog"
KIND_DIGITAL = "digital"
KIND_STDIN = "stdin"

STREAM_PCM = "pcm"
STREAM_ENCODED_IDLE = "encoded-idle"

_IEC61937_PA = 0xF872
_IEC61937_PB = 0x4E1F
_IEC61937_IDLE_TYPES = {0, 3}  # null data, pause
_IEC61937_FORMATS = {
    1: "ac3",
    4: "mpeg", 5: "mpeg", 6: "mpeg", 8: "mpeg", 9: "mpeg",
    7: "aac", 19: "aac", 20: "aac",
    11: "dts", 12: "dts", 13: "dts", 17: "dts",
    21: "eac3",
    22: "truehd",
}
_FULL_SCALE = float(2 ** 31)
_SILENT_DBFS = -math.inf


@dataclass(frozen=True)
class BlockAnalysis:
    peak_dbfs: float
    rms_dbfs: float
    digital_silence: bool
    # Codec name of a detected bitstream, None for PCM.
    encoding: str | None = None
    # Only null/pause bursts: an encoded stream without programme.
    encoded_idle: bool = False


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(value) if value > 0 else _SILENT_DBFS


def detect_encoding(frames: numpy.ndarray) -> tuple[str | None, bool]:
    """Return (codec, idle) for an int32 (n, 2) block.

    IEC 61937 carries 16-bit words in the top bits of each sample: Pa in
    the left and Pb in the right subframe, Pc (data type) in the next left
    subframe.
    """
    if frames.shape[0] < 2:
        return None, False
    words = (frames.astype(numpy.int64) >> 16) & 0xFFFF
    left = words[:, 0]
    right = words[:, 1]
    starts = numpy.flatnonzero((left[:-1] == _IEC61937_PA) & (right[:-1] == _IEC61937_PB))
    if starts.size:
        data_types = [int(value) & 0x1F for value in left[starts + 1]]
        codecs = [_IEC61937_FORMATS.get(value, "encoded")
                  for value in data_types if value not in _IEC61937_IDLE_TYPES]
        if codecs:
            return max(set(codecs), key=codecs.count), False
        return None, True
    flat = words.reshape(-1)
    core_16 = (flat[:-1] == 0x7FFE) & (flat[1:] == 0x8001)
    core_14 = (flat[:-2] == 0x1FFF) & (flat[1:-1] == 0xE800) & ((flat[2:] & 0xFFF0) == 0x07F0)
    if core_16.any() or core_14.any():
        return "dts", False
    return None, False


def analyze_block(frames: numpy.ndarray) -> BlockAnalysis:
    """Analyze one int32 (n, 2) block of interleaved stereo PCM."""
    if frames.size == 0 or not frames.any():
        return BlockAnalysis(_SILENT_DBFS, _SILENT_DBFS, digital_silence=True)
    samples = frames.astype(numpy.float64) / _FULL_SCALE
    peak = float(numpy.max(numpy.abs(samples)))
    rms = float(numpy.max(numpy.sqrt(numpy.mean(samples * samples, axis=0))))
    encoding, encoded_idle = detect_encoding(frames)
    return BlockAnalysis(_dbfs(peak), _dbfs(rms), False, encoding, encoded_idle)


class ActivityDetector:
    """Attack/release state machine for one input.

    ``active`` turns on after ``attack`` seconds of programme signal (gaps
    up to ATTACK_GAP_SECONDS tolerated) and off once no signal was seen for
    the release hold. ``assume_active`` starts it active with a fresh
    hold, for an engagement that must survive a restart.
    """

    def __init__(self, kind: str, now: float, *, assume_active: bool = False) -> None:
        self.kind = kind
        self.active = assume_active
        self.lock: bool | None = None
        self._digital = kind == KIND_DIGITAL
        self._stream = STREAM_PCM
        self._candidate = STREAM_PCM
        self._candidate_blocks = 0
        self._attack_since: float | None = None
        self._last_on_at: float | None = None
        self._last_signal_at = now
        self._missing = False

    @property
    def format(self) -> str | None:
        """Stream type while active: "pcm" or a codec name."""
        return self._stream if self.active and self._stream != STREAM_ENCODED_IDLE else None

    def observe_block(self, now: float, block: BlockAnalysis, lock: bool | None = None) -> None:
        self.lock = lock
        self._missing = False
        if block.digital_silence:
            self._digital = True
        stream = block.encoding or (STREAM_ENCODED_IDLE if block.encoded_idle else STREAM_PCM)
        if stream == self._candidate:
            self._candidate_blocks += 1
        else:
            self._candidate, self._candidate_blocks = stream, 1
        if self._candidate_blocks >= STREAM_CONFIRM_BLOCKS:
            self._stream = self._candidate
        if self._stream == STREAM_ENCODED_IDLE:
            on = keep = False
        elif self._stream != STREAM_PCM:
            on = keep = not block.digital_silence
        elif self._digital:
            on = keep = block.peak_dbfs >= DIGITAL_ON_PEAK_DBFS
        else:
            on = block.rms_dbfs >= ANALOG_ON_RMS_DBFS
            keep = block.peak_dbfs >= ANALOG_KEEP_PEAK_DBFS
        attack = DIGITAL_ATTACK_SECONDS if self._digital or self._stream != STREAM_PCM else ANALOG_ATTACK_SECONDS
        self._step(now, on, keep, attack)

    def mark_digital(self) -> None:
        """The input is known to be digital (S/PDIF by name)."""
        self._digital = True

    def observe_presence(self, now: float, present: bool) -> None:
        """STDIN: a connected PCM writer is the signal."""
        self._missing = False
        self._step(now, present, present, 0.0)

    def observe_missing(self, now: float) -> None:
        """No capture: input gone, probe down or its port not active."""
        self.lock = None
        self._missing = True
        self._attack_since = None
        self.update(now)

    def hold_over(self, now: float) -> None:
        """Pause without observations (measurement): not counted as silence."""
        self._last_signal_at = now
        self._attack_since = None

    def update(self, now: float) -> None:
        if self.active and now - self._last_signal_at >= self._release_hold():
            self.active = False
            self._attack_since = None

    def _release_hold(self) -> float:
        if self.kind == KIND_STDIN:
            return STDIN_RELEASE_SECONDS
        if self._missing:
            return MISSING_RELEASE_SECONDS
        if self.lock is False:
            return UNLOCKED_RELEASE_SECONDS
        return RELEASE_SECONDS

    def _step(self, now: float, on: bool, keep: bool, attack: float) -> None:
        if keep:
            self._last_signal_at = now
        if on:
            if self._attack_since is None:
                self._attack_since = now
            self._last_on_at = now
        elif (self._attack_since is not None and self._last_on_at is not None
              and now - self._last_on_at > ATTACK_GAP_SECONDS):
            self._attack_since = None
        if not self.active and self._attack_since is not None and now - self._attack_since >= attack:
            self.active = True
        self.update(now)
