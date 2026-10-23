# AV Sync accounting

How **Calibration → AV Sync** computes the total electronic audio delay. The
dialog and its everyday use are described in the
[manual](../MANUAL.md#av-sync).

## What is credited

The live engine credits input latency, fixed SRC buffering, DSP processing,
intentional delays and speaker alignment, then adds only the remaining common
delay. Changes to presets, filters, convolver IRs, Loudness FFT, upmixer,
Direct or sample rate update the accounting automatically.

- The input credit includes the observed fixed SRC offset on the routed input
  and hardware paths, including any startup readiness extension retained after
  real audio begins. Engine first-output time is covered by that reserve, not
  added again.
- Unequal-rate SRC currently retains at least **750 ms** of timeline buffering
  in every quality mode; equal nominal rates skip that reserve (see
  [Common playback SRC](playback-src.md#provisional-limits)).
- Digital inputs routed through the Surround helper credit **2818 frames at
  48 kHz (58.71 ms)** for both PCM and AC-3/DTS; direct PCM paths outside that
  helper have no such credit (see [Basic Surround path](surround.md#digital-ingress)).
- Unrelated adapters and paused app taps during an external-input session or
  measurement are not credited.
- Different simultaneous path offsets make live timing unavailable rather than
  selecting a guessed total.
- Device buffering, acoustic travel and variable clock matching are excluded;
  AV Sync does not delay video.

## Capacity

Alignment and automatic compensation share a **500 ms** capacity, including
balancing before mixed Surround sends. A target below the minimum cannot
advance audio; a target above the maximum reaches the delay capacity instead.
The target remains stored so it can become reachable after the chain changes.

## Calibrations and timing balance

Time Alignment, Speaker Auto Alignment and Auto Sub Optimize retain their own
settings. Automatic balancing compensates only electronic timing **changes
since their calibration basis**, never the already measured path spread.

- Successful alignment measurements capture the timing actually used for their
  sweeps.
- Partial measurements keep the other speakers' relative timing.
- Older calibrations without timing evidence safely adopt the verified current
  rendering, without guessing their original DSP setup; the dialog recommends
  remeasurement for a measured basis.

## Convolver IR timing

New correction IRs made by FXRoute carry per-channel timing metadata,
separating algorithmic delay from deliberate aligned-phase delay. An imported
IR without this metadata has unknown timing: the dialog warns that automatic
balancing is unavailable and its reported total is not guaranteed. FXRoute does
not infer delay from an arbitrary IR's length.
