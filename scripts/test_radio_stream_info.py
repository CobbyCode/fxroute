#!/usr/bin/env python3
"""Tests for radio stream_info normalization (player.normalize_stream_info).

Fixtures are live-measured mpv property values from Radio Paradise, FIP,
SomaFM and KEXP streams (2026-08-02).  The tests assert that only values mpv
actually delivered are kept and that no placeholder text is produced.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from player import normalize_stream_info


class StreamInfoNormalizationTests(unittest.TestCase):
    def test_radio_paradise_aac_320(self):
        # live 2026-08-02 18:1x: https://stream.radioparadise.com/aac-320
        raw = {"codec": "AAC (Advanced Audio Coding)", "bitrate_bps": 322074, "samplerate_hz": 44100}
        self.assertEqual(normalize_stream_info(raw), {
            "codec": "AAC", "bitrate_kbps": 322, "samplerate_hz": 44100,
        })

    def test_radio_paradise_flac_lossless(self):
        # live 2026-08-02 18:1x: https://stream.radioparadise.com/flac
        raw = {"codec": "FLAC (Free Lossless Audio Codec)", "bitrate_bps": 717314, "samplerate_hz": 44100}
        self.assertEqual(normalize_stream_info(raw), {
            "codec": "FLAC", "profile": "Lossless", "bitrate_kbps": 717, "samplerate_hz": 44100,
        })

    def test_fip_midfi_mp3_48khz(self):
        # live: https://icecast.radiofrance.fr/fip-midfi.mp3?id=openapi
        raw = {"codec": "MP3 (MPEG audio layer 3)", "bitrate_bps": 128000, "samplerate_hz": 48000}
        self.assertEqual(normalize_stream_info(raw), {
            "codec": "MP3", "bitrate_kbps": 128, "samplerate_hz": 48000,
        })

    def test_somafm_256_mp3(self):
        # live: https://ice4.somafm.com/groovesalad-256-mp3
        raw = {"codec": "MP3 (MPEG audio layer 3)", "bitrate_bps": 256010, "samplerate_hz": 44100}
        self.assertEqual(normalize_stream_info(raw), {
            "codec": "MP3", "bitrate_kbps": 256, "samplerate_hz": 44100,
        })

    def test_kexp_160_aac(self):
        # live 2026-08-02 18:1x: https://kexp.streamguys1.com/kexp160.aac
        raw = {"codec": "AAC (Advanced Audio Coding)", "bitrate_bps": 161162, "samplerate_hz": 44100}
        self.assertEqual(normalize_stream_info(raw), {
            "codec": "AAC", "bitrate_kbps": 161, "samplerate_hz": 44100,
        })

    def test_missing_bitrate_keeps_known_parts(self):
        # robustness: when mpv has not delivered a bitrate yet, the line must
        # stay codec + samplerate instead of dropping everything
        raw = {"codec": "AAC (Advanced Audio Coding)", "samplerate_hz": 44100}
        self.assertEqual(normalize_stream_info(raw), {
            "codec": "AAC", "samplerate_hz": 44100,
        })

    def test_local_flac16_bit_depth(self):
        # live 2026-08-02 18:4x: generated flac16.flac (ffmpeg, 44.1 kHz)
        raw = {"codec": "FLAC (Free Lossless Audio Codec)", "bitrate_bps": 96453,
               "samplerate_hz": 44100, "format": "s16"}
        self.assertEqual(normalize_stream_info(raw), {
            "codec": "FLAC", "profile": "Lossless", "bitrate_kbps": 96,
            "samplerate_hz": 44100, "bit_depth": 16,
        })

    def test_local_flac24_bit_depth_from_decoded_format(self):
        # 24-bit FLAC decodes to s32 in the ffmpeg decoder convention
        raw = {"codec": "FLAC (Free Lossless Audio Codec)", "bitrate_bps": 96530,
               "samplerate_hz": 44100, "format": "s32"}
        self.assertEqual(normalize_stream_info(raw), {
            "codec": "FLAC", "profile": "Lossless", "bitrate_kbps": 97,
            "samplerate_hz": 44100, "bit_depth": 24,
        })

    def test_local_wav24_bit_depth_from_codec_name(self):
        # PCM depth is parsed from the mpv codec name, not from the URL
        raw = {"codec": "PCM signed 24-bit little-endian", "bitrate_bps": 1058400,
               "samplerate_hz": 44100, "format": "s32"}
        self.assertEqual(normalize_stream_info(raw), {
            "codec": "PCM", "bitrate_kbps": 1058,
            "samplerate_hz": 44100, "bit_depth": 24,
        })

    def test_local_mp3_no_bit_depth(self):
        # lossy decodes to floatp; no source bit depth is shown
        raw = {"codec": "MP3 (MPEG audio layer 3)", "bitrate_bps": 319985,
               "samplerate_hz": 44100, "format": "floatp"}
        self.assertEqual(normalize_stream_info(raw), {
            "codec": "MP3", "bitrate_kbps": 320, "samplerate_hz": 44100,
        })

    def test_local_opus_48000(self):
        raw = {"codec": "Opus (Opus Interactive Audio Codec)", "bitrate_bps": 70800,
               "samplerate_hz": 48000, "format": "floatp"}
        self.assertEqual(normalize_stream_info(raw), {
            "codec": "Opus", "bitrate_kbps": 71, "samplerate_hz": 48000,
        })

    def test_empty_and_unknown_parts_dropped(self):
        self.assertIsNone(normalize_stream_info(None))
        self.assertIsNone(normalize_stream_info({}))
        # Only a bitrate without codec is not enough for a tech line.
        self.assertIsNone(normalize_stream_info({"bitrate_bps": 128000}))
        # Zero/negative values are dropped; the known codec stays.
        self.assertEqual(normalize_stream_info(
            {"codec": "AAC (Advanced Audio Coding)", "bitrate_bps": 0, "samplerate_hz": -1}
        ), {"codec": "AAC"})

    def test_unknown_codec_kept_uppercased(self):
        self.assertEqual(normalize_stream_info({"codec": "opus"}), {"codec": "Opus"})
        self.assertEqual(normalize_stream_info({"codec": "Vorbis"}), {"codec": "Vorbis"})


if __name__ == "__main__":
    unittest.main()
