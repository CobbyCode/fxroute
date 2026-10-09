# TCL channel-switch regression carriers

Four losslessly gzip-compressed, little-endian stereo S32 extracts from the
authoritative 22-channel, 48-kHz RT recording of real TV channel switches.
`manifest.json` identifies the source recording by SHA256, original frame
bounds, raw lanes (0/1), the first new preamble, and each decompressed hash.
These are carrier samples, not a decoded programme recording.

Each extract contains preceding AC-3 bursts, the complete switch gap, the
orphan payload prefix, and three valid subsequent stereo AC-3 bursts.
The prefixes previously escaped at almost full scale on both output taps.
Tests replay the original DD-gap-DD context and reuse the captured orphan
prefix after established PCM. The latter also covers the PCM-to-DD acquisition
boundary without pretending that the four original events were PCM-origin.

To reproduce an extract from `record.f32`, reshape little-endian float32 into
22 lanes, select lanes 0/1 and the half-open source frame bounds in the manifest,
round samples multiplied by 2147483648 to nearest-even, clamp to the signed
32-bit range, and write little-endian int32. Gzip with `mtime=0`.

Run `python3 scripts/test_surround_ingress.py` on a host with native Surround
build dependencies and FFmpeg. All four cases exercise discrete/stereo output
and chunk sizes 1, 257, and 1024; the valid post-preamble audio is checked
against FFmpeg independently.
