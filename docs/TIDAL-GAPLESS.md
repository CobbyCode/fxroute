# TIDAL gapless playback investigation

## Reproduction

Album: **Champion Sound (Live from Prague)**, Smif-N-Wessun & Champion Sound.
The authenticated native TIDAL provider used PKCE lossless streams, materialized
as fragmented MP4 files containing stereo 44.1 kHz / 16-bit FLAC.

Transitions checked:

- 194408419 → 194408420: Bucktown USA (Champion Sound Intro) → Let's Git It On
- 194408420 → 194408421: Let's Git It On → Stand Strong
- 194408421 → 194408422: Stand Strong → I Love You (Remix)

## Cause

The local Library supplies a homogeneous native MPV playlist. Natural boundaries
retain MPV's audio output and only update the app's queue index/track metadata.
TIDAL instead downloaded the next DASH stream into the cache, but left MPV with
only the current entry. After EOF, app-owned queue advancement resolved the
stream again and entered the Coordinator's pause, volume-zero, replace-load and
hardware-gate path. A cache hit did not prevent that interruption.

PCM captures at DSP ingress already contained 789.48, 789.48 and 743.04 ms of
silence at the three boundaries. DSP and output-monitor captures showed the same
interruptions. Independently decoded source files contained continuous audio at
their starts/ends, not these inserted silent intervals.

MPV also emits a transient `path=None`/EOF while opening an appended successor.
Treating that event as terminal races the native boundary and replaces an
already-opening track. `idle-active=True`, not the transient path clear, confirms
that MPV exhausted its playlist.

## Correction

- Resolve one successor while the current track plays; atomically published DASH
  files and the existing per-key materialization locks remain unchanged.
- Append the resolved successor under the manual transport guard only when the
  queue, source intent, owner, playback epoch and live MPV path still match.
- Compare its source rate with MPV's **decoded** rate, not a fixed hardware/SRC
  rate. Unknown/different source rates retain coordinated navigation.
- Let MPV own the natural boundary. Publish the prepared successor's queue
  identity and metadata without pausing, replacing, muting or rebuilding DSP.
- Confirm MPV playlist entry IDs separately from paths. Repeated tracks cannot
  advance on an unchanged path, and delayed callbacks cannot publish after a
  source takeover. Confirmed native entries survive non-reloading graph changes,
  but are scoped to the MPV wrapper and its process generation.
- Expose coherent `idle_active` state and notify terminal idle even when the
  path was already cleared. This preserves EOF fallback after failed opening.
- Enable MPV playlist prefetch so queued network streams open before the current
  track ends, instead of spending the output buffer on HTTP/decoder startup.
- Invalidate delayed results on queue/source/epoch changes. Clear only queued
  entries on queue reset, shuffle and loop changes; keep current audio running.
- Reconcile already-started entries before navigation or queue mutations.
  Library scans prune local IDs only, not TIDAL entries. Brief decoder-rate
  unreadiness permits at most three preparation attempts in the same context.
- Verify which entry MPV retained after playlist clearing, including an autonomous
  boundary during IPC or a lost append response. Keep cleanup ownership on failed
  verification; do not report a single-track play as successful with a successor
  still queued. A same-target play overtaken by a native boundary returns a retry
  conflict instead of publishing the wrong track.
- Reconcile pause/toggle/stop while holding their transport guard. Reject obsolete
  EOF snapshots against live player state again after the transition waiter,
  even if loop/shuffle already retired the predecessor's prepared mapping.

## Measured verification

For all three transitions, captures after the correction show **0 inserted
samples** between the last second of the old track and the first second of the
new track. The ingress PCM matches both independently decoded references exactly
(RMS error 0). DSP and hardware-output monitor captures contain no silent run
longer than 10 ms at any boundary. No hardware-gate closure was observed.

The short boundary captures used seeking to the last eight seconds, followed by
a natural MPV transition; they are not a crossfade or a manual Next test. Two
additional uninterrupted runs played from the first track through all three
boundaries in approximately 713 seconds, without seeking or manual navigation.
Each uninterrupted boundary also matched the independently decoded ingress PCM
exactly, with zero inserted samples, no gate closures and unchanged MPV/DSP node
identities. Capture startup silence is excluded from the boundary measurement.

Regression coverage:

- `scripts/test_tidal_gapless_queue.py`: rolling successors, multiple boundaries,
  stale resolution, source takeover, decoded-rate fallback, pause, reset, loop,
  shuffle, manual retry, playlist-clear boundary races and unconfirmed-append
  cleanup, including delayed event delivery and retained cleanup ownership.
- `scripts/test_tidal_prefetch.py`: app callback integration, transient native EOF
  and genuine terminal-idle fallback using the real MPV wrapper event handling;
  guarded transport publication, late prefetch trimming, failed cleanup, delayed
  predecessor EOF after loop/shuffle, and reused playlist IDs after MPV restart.
- `scripts/test_mpv_stream_prefetch.py`: real isolated MPV with a delayed synthetic
  HTTP stream; the queued stream opens while the current track still plays.
  Removing playlist prefetch makes this test fail. No real audio output is used.

The real-album evidence applies to the PKCE/FLAC path above. The HTTP fixture
checks network-prefetch behavior, not real TIDAL device-login/AAC streams.
