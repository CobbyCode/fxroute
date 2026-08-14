# Loudness Strength transient — findings and fix

Status: resolved. This file records the diagnosis and the verified fix.

## Symptom

Changing Loudness Strength, especially 1 -> 5, produced a short, reproducible
positive level excursion (a brief gain bump) at the pre-master meter tap.

## Intended invariant

The Loudness stage is one level-neutral transaction:

- LSP loud_comp_stereo work point `p` (`volume` control)
- host compensation `-p`
- any associated AutoGain contribution

All parts must become effective on the same audio-block boundary. A Strength
change alters the curve but must not create a temporary positive gain
excursion.

Work-point formula (unchanged):

```
p = volumeDb - calibration + (10 - strength) * 30/9 + AutoGain
```

| strength | strength_db | p      |
|----------|-------------|--------|
| 1        | 30.00       | -10.00 |
| 2        | 26.67       | -13.33 |
| 3        | 23.33       | -16.67 |
| 4        | 20.00       | -20.00 |
| 5        | 16.67       | -23.33 |

1 -> 5 lowers `p`; 5 -> 4 raises `p`.

## LSP loud_comp_stereo FFT mechanics (measured on lsp-plugins 1.2.33)

The FFT mode routes the signal through `dspu::SpectralProcessor`:

- rank = 8 + fft index (fft 0..6 -> rank 8..14 -> FFT size 256..16384).
- hop = `frame_size = 2^(rank-1)`; window = LSP `cosine` window
  (`w[n] = sin(pi*n/N)`, N = `2^rank`) applied twice = squared Hann, 50%
  overlap-add.
- `SpectralProcessor::latency() = 1 << nRank = 2^rank` (the plugin reports
  this and delays its own dry/bypass path by the same amount).
- A `volume` (work-point) change recomputes the frequency-domain gain mesh
  (`vFreqApply`) before the next `run()`. The new mesh is applied at the next
  FFT frame boundary and reaches the output as a crossfade over one frame
  (`frame_size` samples) with the squared cosine overlap weight
  `sin^2(pi*d/buf_size)`.
- Left and right channels are phase-staggered by half a frame
  (`set_phase(0.5*i)`), so the L and R crossfades start `frame_size/2`
  samples apart.

## Root cause

The `volume` work point changes through the FFT/OLA path with the timing
above, while the host compensation is a flat multiply. Applied on either the
plugin input or output with zero/instant timing, the two cannot cancel during
the window where one has switched and the other has not, so `|p_new - p_old|`
is briefly exposed. Moving the compensation from output to input only flips
which Strength direction produces the positive excursion.

## Fix

The compensation is applied after the plugin (post-LSP) and is crossfaded in
lock-step with the work-point curve instead of switching instantly:

1. The host derives the frame/buffer sizes from the stage `fft` control.
2. It tracks the cumulative samples fed to the LV2 stage, so it can compute
   the next FFT boundary per channel (L phase 0, R phase `frame_size/2`).
3. On a compensation change it keeps the previous trim until that boundary,
   then crossfades to the new trim with the same squared cosine overlap, and
   applies the reciprocal of the linearly-crossfaded work-point gain so the
   pair stays level-neutral on every sample of the transition.

The pre-master meter tap stays between the level-neutral
Loudness+compensation pair and the canonical master_gain; the canonical
listening attenuation and the protection limiter are unchanged.

## Verification

`native_dsp/test_loudness_strength.c` drives the real live-update sequence
while a concurrent thread runs `fxdsp_process`, steps Strength through both
directions (1->5, 5->1, 5->4, 4->5 and the full adjacent sweep) and checks the
maximum intermediate output peak. It runs at the default 4096 FFT, the
smallest 256 FFT (boundaries inside a block), the largest 16384 FFT and at
44.1/48/96 kHz. All configurations pass with the worst intermediate gain at
or below 0.02 dB (tolerance 0.25 dB); the pre-fix code exposed the full
work-point delta (about 3.3 dB per adjacent step).
