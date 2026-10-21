#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise channel fallback with real MPV IPC and a silent null output."""
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playback.player import MPVWrapper


def write_pcm(path, channels, mask):
    """Write silent WAVE_FORMAT_EXTENSIBLE PCM with explicit speaker positions."""
    fmt = struct.pack('<HHIIHHHHI16s', 0xfffe, channels, 48000, 48000 * channels * 2,
                      channels * 2, 16, 22, 16, mask,
                      bytes.fromhex('0100000000001000800000aa00389b71'))
    pcm = bytes(48000 * channels * 2)
    body = b'WAVEfmt ' + struct.pack('<I', len(fmt)) + fmt + b'data' + struct.pack('<I', len(pcm)) + pcm
    path.write_bytes(b'RIFF' + struct.pack('<I', len(body)) + body)


@unittest.skipUnless(shutil.which('mpv'), 'mpv binary is not installed')
class MPVLayoutFallbackTests(unittest.TestCase):
    def wait_for(self, predicate):
        deadline = time.monotonic() + 8
        while not predicate():
            self.assertLess(time.monotonic(), deadline, 'MPV channel layout did not settle')
            time.sleep(.02)

    def test_unsupported_layout_downmix_and_native_playlist_surround_restore(self):
        for channels, mask in ((7, 0x70f), (8, 0xff)):
            with self.subTest(channels=channels, mask=mask), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                unsupported, supported = root / 'unsupported.wav', root / 'supported.wav'
                write_pcm(unsupported, channels, mask)
                write_pcm(supported, 6, 0x60f)
                player = MPVWrapper()
                player.socket_path = str(root / 'mpv.sock')
                command = [arg for arg in player._mpv_command(player.socket_path)
                           if not arg.startswith('--audio-device=')]
                player.process = subprocess.Popen(command + ['--no-config', '--ao=null', '--pause'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    self.wait_for(lambda: Path(player.socket_path).exists())
                    player._running = True
                    player._listener_thread = threading.Thread(target=player._event_listener_loop, daemon=True)
                    player._listener_thread.start()
                    self.wait_for(lambda: player._listener_socket is not None)
                    player.set_surround_channels(True)
                    player.loadfile(str(unsupported), start_paused=True)
                    self.wait_for(lambda: (player.state.get('audio_output_params') or {}).get('channel-count') == 2)
                    self.assertEqual(player.get_property('audio-out-params')['channels'], 'stereo')
                    player.loadfile(str(supported), 'append')
                    player._send_command('playlist-next', 'force')
                    self.wait_for(lambda: (player.state.get('audio_output_params') or {}).get('channel-count') == 6)
                    self.assertEqual(player.get_property('audio-out-params')['channels'], '5.1(side)')
                    entry = player.get_property('playlist-pos')
                    player.set_surround_channels(False)
                    self.wait_for(lambda: (player.state.get('audio_output_params') or {}).get('channel-count') == 2)
                    player.set_surround_channels(True)
                    self.wait_for(lambda: (player.state.get('audio_output_params') or {}).get('channel-count') == 6)
                    self.assertEqual(player.get_property('path'), str(supported))
                    self.assertEqual(player.get_property('playlist-pos'), entry)
                    self.assertTrue(player.get_property('pause'))
                finally:
                    player.stop()


if __name__ == '__main__':
    unittest.main()
